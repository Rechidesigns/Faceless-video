"""Users and sessions: email + password (argon2), server-side sessions, login throttling.

OTP / email verification can be added later using User.email_verified.
"""
import hashlib
import re
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from . import config, db

ph = PasswordHasher()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
SESSION_DAYS = 30
COOKIE = "fs_session"


class AuthError(Exception):
    pass


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_dict(u: db.User) -> dict:
    return {"id": u.id, "email": u.email, "is_admin": u.is_admin, "is_active": u.is_active,
            "is_approved": u.is_approved, "email_verified": u.email_verified,
            "daily_limit": u.daily_limit, "created_at": u.created_at}


PENDING_MSG = "Your account is waiting for approval. You'll be able to sign in once the admin approves it."


def approval_required() -> bool:
    return config.flag("REQUIRE_APPROVAL", True)


# ---------- throttling (in memory, per email and per IP) ----------
_fails: dict[str, list[float]] = {}
_fail_lock = threading.Lock()
MAX_FAILS, WINDOW = 8, 15 * 60


def _throttled(*keys: str) -> bool:
    now = time.time()
    with _fail_lock:
        for k in keys:
            _fails[k] = [t for t in _fails.get(k, []) if now - t < WINDOW]
            if len(_fails[k]) >= MAX_FAILS:
                return True
    return False


def _record_fail(*keys: str) -> None:
    with _fail_lock:
        for k in keys:
            _fails.setdefault(k, []).append(time.time())


# ---------- users ----------
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def signup_open() -> bool:
    with db.session() as s:
        if s.query(db.User).count() == 0:
            return True  # the very first account can always be created (it becomes admin)
    return config.flag("ALLOW_SIGNUP", True)


def create_user(email: str, password: str) -> tuple[dict, bool]:
    """Returns (user, is_first_user)."""
    email = normalize_email(email)
    if not EMAIL_RE.match(email) or len(email) > 320:
        raise AuthError("Enter a valid email address")
    if len(password or "") < 8:
        raise AuthError("Password must be at least 8 characters")
    if len(password) > 256:
        raise AuthError("Password is too long")
    admin_email = normalize_email(config.env("ADMIN_EMAIL"))
    with db.session() as s:
        if s.query(db.User).filter(db.User.email == email).first():
            raise AuthError("An account with this email already exists. Sign in instead.")
        first = s.query(db.User).count() == 0
        is_admin = email == admin_email if admin_email else first
        u = db.User(email=email, password_hash=ph.hash(password), is_admin=is_admin,
                    is_approved=is_admin or not approval_required())
        s.add(u)
        s.flush()
        return user_dict(u), first


def authenticate(email: str, password: str, ip: str = "") -> dict:
    email = normalize_email(email)
    keys = (f"e:{email}", f"ip:{ip}")
    if _throttled(*keys):
        raise AuthError("Too many attempts. Try again in 15 minutes.")
    with db.session() as s:
        u = s.query(db.User).filter(db.User.email == email).first()
        ok = False
        if u:
            try:
                ok = ph.verify(u.password_hash, password or "")
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                ok = False
            if ok and ph.check_needs_rehash(u.password_hash):
                u.password_hash = ph.hash(password)
        else:
            ph.hash("timing-equalizer")  # similar response time for unknown emails
        if not ok:
            _record_fail(*keys)
            raise AuthError("Wrong email or password")
        if not u.is_active:
            raise AuthError("This account is disabled")
        if not u.is_approved:
            raise AuthError(PENDING_MSG)
        return user_dict(u)


def ensure_admin(email: str, password: str) -> str:
    """Create the account as an approved admin, or promote an existing one. Used by `python -m app.admin_cli`."""
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address")
    with db.session() as s:
        u = s.query(db.User).filter(db.User.email == email).first()
        if u:
            u.is_admin = u.is_approved = u.is_active = True
            if password:
                u.password_hash = ph.hash(password)
            return "promoted existing account to admin" + (" and set a new password" if password else "")
        if len(password or "") < 8:
            raise AuthError("Password must be at least 8 characters")
        s.add(db.User(email=email, password_hash=ph.hash(password), is_admin=True, is_approved=True))
        return "created admin account"


def change_password(user_id: str, old: str, new: str) -> None:
    if len(new or "") < 8:
        raise AuthError("New password must be at least 8 characters")
    with db.session() as s:
        u = s.get(db.User, user_id)
        try:
            ph.verify(u.password_hash, old or "")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            raise AuthError("Current password is wrong")
        u.password_hash = ph.hash(new)
        # sign out other sessions
        s.query(db.AuthSession).filter(db.AuthSession.user_id == user_id).delete()


# ---------- sessions ----------
def new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.session() as s:
        s.add(db.AuthSession(token_hash=_hash_token(token), user_id=user_id,
                             created_at=now, expires_at=now + SESSION_DAYS * 86400))
        # opportunistic cleanup of expired sessions
        s.query(db.AuthSession).filter(db.AuthSession.expires_at < now).delete()
    return token


def user_for_token(token: str) -> dict | None:
    if not token:
        return None
    with db.session() as s:
        row = s.get(db.AuthSession, _hash_token(token))
        if not row or row.expires_at < time.time():
            return None
        u = s.get(db.User, row.user_id)
        if not u or not u.is_active or not u.is_approved:
            return None
        return user_dict(u)


def end_session(token: str) -> None:
    if token:
        with db.session() as s:
            s.query(db.AuthSession).filter(db.AuthSession.token_hash == _hash_token(token)).delete()


# ---------- limits ----------
def daily_limit(user: dict) -> int:
    """0 = unlimited."""
    if user.get("is_admin"):
        return 0
    if user.get("daily_limit") is not None:
        return int(user["daily_limit"])
    try:
        return int(config.get("DAILY_VIDEO_LIMIT") or 3)
    except ValueError:
        return 3


def videos_today(user_id: str) -> int:
    with db.session() as s:
        return s.query(db.Video).filter(db.Video.user_id == user_id,
                                        db.Video.created_at > time.time() - 86400).count()
