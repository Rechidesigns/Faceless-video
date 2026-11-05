"""Web server: accounts, UI, JSON API, OAuth callbacks, scheduler startup."""
import hashlib
import hmac
import threading
import time

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (auth, config, db, jobs, llm, migrate, pipeline, publish, scheduler, storage, store,
               topics, tts, visuals)

app = FastAPI(title="Faceless Shorts")
app.mount("/static", StaticFiles(directory=config.STATIC), name="static")


@app.on_event("startup")
def _startup():
    db.init()
    migrate.resume_if_needed()
    # BACKGROUND_TASKS=false on a second instance (e.g. your PC) that shares the production database:
    # only one instance may run the scheduler and recover interrupted jobs, or scheduled videos get made twice.
    if config.flag("BACKGROUND_TASKS", True):
        jobs.startup()
        scheduler.start()
    else:
        print("BACKGROUND_TASKS=false: scheduler and restart recovery are off on this instance")


# ---------- authentication ----------
OPEN_PATHS = {"/login", "/api/auth/login", "/api/auth/signup", "/api/auth/info", "/healthz", "/privacy", "/terms"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    publish.remember_base(str(request.base_url).rstrip("/"))
    path = request.url.path
    request.state.user = None
    if path in OPEN_PATHS or path.startswith("/pub/") or path.startswith("/static/"):
        return await call_next(request)
    user = auth.user_for_token(request.cookies.get(auth.COOKIE, ""))
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Login required"}, status_code=401)
        return RedirectResponse("/login")
    request.state.user = user
    return await call_next(request)


def current_user(request: Request) -> dict:
    return request.state.user


def admin_user(request: Request) -> dict:
    u = request.state.user
    if not u or not u.get("is_admin"):
        raise HTTPException(403, "Admins only")
    return u


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


def _session_response(user: dict, request: Request, extra: dict | None = None) -> JSONResponse:
    resp = JSONResponse({"ok": True, "user": user, **(extra or {})})
    resp.set_cookie(auth.COOKIE, auth.new_session(user["id"]), max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return resp


@app.get("/login")
def login_page():
    return FileResponse(config.STATIC / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/api/auth/info")
def auth_info():
    return {"signup_open": auth.signup_open(), "approval_required": auth.approval_required()}


_signup_lock = threading.Lock()


@app.post("/api/auth/signup")
def signup(body: Credentials, request: Request):
    with _signup_lock:  # so two people can't both become "the first user"
        if not auth.signup_open():
            raise HTTPException(403, "Sign-ups are closed. Ask the admin for an account.")
        try:
            user, first = auth.create_user(body.email, body.password)
        except auth.AuthError as e:
            raise HTTPException(400, str(e))
    if not user["is_approved"]:
        return JSONResponse({"ok": True, "pending": True, "message":
                             "Account created! The admin will review it. You'll be able to sign in once it's approved."})
    extra = {}
    if first:
        extra["imported"] = migrate.import_legacy(user["id"])
    return _session_response(user, request, extra)


@app.post("/api/auth/login")
def login(body: Credentials, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
    try:
        user = auth.authenticate(body.email, body.password, ip)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    return _session_response(user, request)


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


class PasswordChange(BaseModel):
    old: str
    new: str = Field(max_length=256)


@app.post("/api/auth/password")
def change_password(body: PasswordChange, user: dict = Depends(current_user)):
    try:
        auth.change_password(user["id"], body.old, body.new)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    out = {**user, "videos_today": auth.videos_today(user["id"]), "limit": auth.daily_limit(user)}
    if user["is_admin"]:
        with db.session() as s:
            out["pending_users"] = s.query(db.User).filter(db.User.is_approved.is_(False),
                                                           db.User.is_active.is_(True)).count()
    return out


@app.get("/healthz")
def health():
    return {"ok": True}


# ---------- public pages that YouTube/TikTok/Instagram app registration asks for ----------
_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><body style="font:16px/1.6 system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#1d222c">
<h1>{title}</h1>{body}<p style="color:#888">Contact: {contact}</p></body>"""


@app.get("/privacy")
def privacy():
    body = """<p>Faceless Shorts lets registered users create short videos and publish them to social accounts they connect.</p>
<p><b>Account data:</b> your email address and a securely hashed password. We never store your password in plain text.</p>
<p><b>Connected accounts:</b> when you connect YouTube, TikTok or Instagram, we receive access tokens and basic profile info
(channel or account name) solely to upload videos you create to that account. Disconnecting an account deletes its tokens;
you can also revoke access in your Google, TikTok or Instagram settings.</p>
<p><b>Videos:</b> generated videos are stored privately and only accessible to your account through temporary links.</p>
<p><b>Third parties:</b> scripts are written with AI services (Google Gemini, Groq); footage comes from Pixabay/Pexels and AI
images from Cloudflare Workers AI. No personal data is sent to them. We do not sell or share your data.</p>
<p>To delete your account and data, contact us at the address below.</p>"""
    return HTMLResponse(_PAGE.format(title="Privacy Policy", body=body, contact=config.get("CONTACT_EMAIL") or "the site owner"))


@app.get("/terms")
def terms():
    body = """<p>Faceless Shorts is a tool for creating short-form videos and publishing them to accounts you control.</p>
<p>You are responsible for the content you publish and for following YouTube's, TikTok's and Instagram's terms and community
guidelines. Content may be AI-assisted and is labelled as such where the platform supports it.</p>
<p>Usage limits apply. Accounts that abuse the service may be disabled. The service is provided as is, without warranty.</p>"""
    return HTMLResponse(_PAGE.format(title="Terms of Service", body=body, contact=config.get("CONTACT_EMAIL") or "the site owner"))


@app.get("/pub/{job_id}/{exp}/{sig}/video.mp4")
def public_video(job_id: str, exp: int, sig: str):
    """Temporary signed link (local storage only) so Instagram can fetch a video."""
    if exp < time.time() or not hmac.compare_digest(sig, publish.sign(job_id, exp)):
        raise HTTPException(403, "Link expired")
    job = jobs.get(job_id)
    if not job or not job.get("blob_key") or storage.remote():
        raise HTTPException(404, "Not found")
    path = storage.local_path(job["blob_key"])
    if not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="video/mp4")


# ---------- models ----------
class TopicIn(BaseModel):
    topic: str = Field(min_length=2, max_length=500)
    duration: int = Field(45, ge=15, le=90)
    style: str = Field("engaging, curious", max_length=100)
    language: str = Field("English", max_length=40)
    channel_id: str | None = None


class RenderSettings(BaseModel):
    voice: str = Field("en-US-AndrewMultilingualNeural", max_length=100)
    rate: str = Field("+5%", max_length=8)
    visual_mode: str = Field("mixed", pattern="^(stock|ai|mixed)$")
    caption_style: str = Field("bold", pattern="^(bold|minimal|none)$")
    music: str = Field("random", max_length=200)
    music_volume: float = Field(0.12, ge=0, le=1)


class RenderIn(BaseModel):
    script: dict
    settings: RenderSettings
    channel_id: str | None = None


class AutoIn(BaseModel):
    topics: list[str] = Field(max_length=20)
    duration: int = Field(45, ge=15, le=90)
    style: str = Field("engaging, curious", max_length=100)
    language: str = Field("English", max_length=40)
    settings: RenderSettings
    channel_id: str | None = None


def _channel_ctx(cid: str | None, user: dict) -> dict:
    if not cid:
        return {}
    ch = store.get_channel(cid, user["id"])
    if not ch:
        raise HTTPException(404, "No such channel")
    return {"niche": ch["niche"], "notes": ch.get("notes", "")}


# Light per-user limit on script-only requests (they use the shared AI keys too).
_script_calls: dict[str, list[float]] = {}


def _script_quota(user: dict) -> None:
    if user.get("is_admin"):
        return
    now = time.time()
    calls = [t for t in _script_calls.get(user["id"], []) if now - t < 86400]
    limit = max(10, auth.daily_limit(user) * 5) if auth.daily_limit(user) else 0
    if limit and len(calls) >= limit:
        raise HTTPException(429, "Daily script limit reached. Try again later.")
    calls.append(now)
    _script_calls[user["id"]] = calls


# ---------- core routes ----------
@app.get("/")
def index():
    return FileResponse(config.STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status(user: dict = Depends(current_user)):
    out = {
        "music": pipeline.music_files(),
        "youtube_ready": publish.youtube_configured(),
        "tiktok_ready": publish.tiktok_configured(),
        "instagram_ready": publish.instagram_configured(),
    }
    if user.get("is_admin"):
        out.update(keys={k: bool(config.get(k)) for k in config.KEYS}, ffmpeg=bool(config.FFMPEG),
                   storage=storage.describe(), database="postgres" if not db.IS_SQLITE else "sqlite")
    return out


@app.get("/api/voices")
async def voices():
    try:
        return await tts.list_voices()
    except Exception as e:
        raise HTTPException(502, f"Could not load voices: {e}")


@app.get("/api/voice-preview")
def voice_preview(voice: str, rate: str = "+0%"):
    folder = config.DATA / ".cache"
    folder.mkdir(exist_ok=True)
    out = folder / f"preview_{hashlib.sha1(f'{voice}|{rate}'.encode()).hexdigest()[:16]}.mp3"
    if not out.exists() or out.stat().st_size < 1000:
        tmp = out.with_suffix(f".{time.time_ns()}.tmp")
        try:
            # one quick retry only: the user is waiting on the button
            tts.synth("Did you know? This is how your next short will sound.", voice[:100], rate[:8], tmp,
                      waits=(2,), timeout=25)
            tmp.replace(out)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise HTTPException(502, "Couldn't reach the voice service. Check your internet connection and try again.")
    return FileResponse(out, media_type="audio/mpeg")


@app.post("/api/script")
def write_script(body: TopicIn, user: dict = Depends(current_user)):
    ctx = _channel_ctx(body.channel_id, user)
    _script_quota(user)
    try:
        return llm.generate_script(body.topic, body.duration, body.style, body.language,
                                   ctx.get("niche", ""), ctx.get("notes", ""))
    except Exception as e:
        raise HTTPException(502, str(e))


def _submit(user, **kw):
    try:
        return jobs.public_view(jobs.submit(user, **kw))
    except jobs.LimitError as e:
        raise HTTPException(429, str(e))
    except KeyError:
        raise HTTPException(404, "No such channel")


@app.post("/api/render")
def render_video(body: RenderIn, user: dict = Depends(current_user)):
    if not body.script.get("scenes"):
        raise HTTPException(400, "Script has no scenes")
    return _submit(user, settings=body.settings.model_dump(), script=body.script, channel_id=body.channel_id)


@app.post("/api/auto")
def auto(body: AutoIn, user: dict = Depends(current_user)):
    items = [t.strip()[:500] for t in body.topics if t.strip()][:20]
    if not items:
        raise HTTPException(400, "Add at least one topic")
    ctx = _channel_ctx(body.channel_id, user)
    out = []
    for t in items:
        try:
            out.append(jobs.public_view(jobs.submit(
                user, body.settings.model_dump(), channel_id=body.channel_id,
                topic={"topic": t, "duration": body.duration, "style": body.style, "language": body.language, **ctx})))
        except jobs.LimitError as e:
            if not out:
                raise HTTPException(429, str(e))
            break  # queue what fits in today's limit
    return out


# ---------- videos ----------
@app.get("/api/jobs")
def list_jobs(channel: str | None = None, user: dict = Depends(current_user)):
    return [jobs.public_view(j) for j in jobs.list_for(user["id"], channel)]


def _job_call(fn, *args):
    try:
        return jobs.public_view(fn(*args))
    except KeyError:
        raise HTTPException(404, "No such video")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, user: dict = Depends(current_user)):
    return _job_call(jobs.retry, job_id, user["id"])


@app.post("/api/jobs/{job_id}/publish/{platform}")
def publish_job(job_id: str, platform: str, user: dict = Depends(current_user)):
    if platform not in publish.PLATFORMS:
        raise HTTPException(404, "Unknown platform")
    return _job_call(jobs.approve, job_id, user["id"], platform)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, user: dict = Depends(current_user)):
    try:
        jobs.delete(job_id, user["id"])
    except KeyError:
        raise HTTPException(404, "No such video")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.get("/api/videos/{job_id}/file")
def video_file(job_id: str, download: int = 0, user: dict = Depends(current_user)):
    """Stream / download a video. With a bucket, redirects to a short-lived signed URL."""
    job = jobs.get(job_id, user["id"])
    if not job or not job.get("blob_key"):
        raise HTTPException(404, "Video not found")
    name = "".join(c for c in (job.get("title") or "short") if c.isalnum() or c in " -_").strip()[:80] or "short"
    url = storage.presigned_url(job["blob_key"], 3600, f"{name}.mp4" if download else None)
    if url:
        return RedirectResponse(url, status_code=307)
    path = storage.local_path(job["blob_key"])
    if not path.exists():
        raise HTTPException(404, "Video file missing")
    return FileResponse(path, media_type="video/mp4", filename=f"{name}.mp4" if download else None)


# ---------- channels ----------
def _channel_out(ch: dict) -> dict:
    return {**{k: v for k, v in ch.items() if k != "user_id"},
            "next_run": scheduler.next_run(ch), "connected": store.connected(ch["id"])}


def _own_channel(cid: str, user: dict) -> dict:
    ch = store.get_channel(cid, user["id"])
    if not ch:
        raise HTTPException(404, "No such channel")
    return ch


@app.get("/api/channels")
def channels(user: dict = Depends(current_user)):
    return [_channel_out(c) for c in store.list_channels(user["id"])]


@app.post("/api/channels")
def create_channel(body: dict, user: dict = Depends(current_user)):
    if len(store.list_channels(user["id"])) >= 20 and not user["is_admin"]:
        raise HTTPException(400, "Channel limit reached (20)")
    return _channel_out(store.save_channel(body, user["id"]))


@app.put("/api/channels/{cid}")
def update_channel(cid: str, body: dict, user: dict = Depends(current_user)):
    try:
        return _channel_out(store.save_channel(body, user["id"], cid))
    except KeyError:
        raise HTTPException(404, "No such channel")


@app.delete("/api/channels/{cid}")
def remove_channel(cid: str, user: dict = Depends(current_user)):
    _own_channel(cid, user)
    store.delete_channel(cid, user["id"])
    return {"ok": True}


@app.post("/api/channels/{cid}/topics")
def channel_topics(cid: str, user: dict = Depends(current_user)):
    _own_channel(cid, user)
    _script_quota(user)
    try:
        topics.refill(cid, 10)
    except Exception as e:
        raise HTTPException(502, str(e))
    return _channel_out(store.get_channel(cid))


@app.post("/api/channels/{cid}/run")
def channel_run(cid: str, user: dict = Depends(current_user)):
    _own_channel(cid, user)
    try:
        return jobs.public_view(scheduler.run_channel(cid, trigger="manual", user=user))
    except jobs.LimitError as e:
        raise HTTPException(429, str(e))


@app.post("/api/channels/{cid}/disconnect/{platform}")
def channel_disconnect(cid: str, platform: str, user: dict = Depends(current_user)):
    _own_channel(cid, user)
    store.del_token(cid, platform)
    store.update_channel(cid, lambda c: c.setdefault(platform, {}).update(account=None))
    return _channel_out(store.get_channel(cid))


# ---------- admin: settings & users ----------
def _mask(v: str) -> str:
    return "••••" + v[-4:] if len(v) > 8 else "••••"


@app.get("/api/settings")
def get_settings(request: Request, _: dict = Depends(admin_user)):
    base = publish.public_base() or str(request.base_url).rstrip("/")
    out = {}
    for k, meta in config.EDITABLE.items():
        v = config.get(k)
        out[k] = {**meta, "set": bool(v), "source": config.source(k),
                  "value": (_mask(v) if meta["secret"] else v) if v else ""}
    return {"settings": out, "base_url": base,
            "redirects": {p: f"{base}/oauth/{p}/callback" for p in publish.PLATFORMS},
            "storage": storage.describe(), "database": "postgres" if not db.IS_SQLITE else "sqlite"}


@app.put("/api/settings")
def put_settings(body: dict, _: dict = Depends(admin_user)):
    config.save_overrides({k: v for k, v in body.items() if k in config.EDITABLE})
    return {"ok": True}


def _check(fn) -> dict:
    try:
        return {"ok": True, "msg": fn()}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


def _need(name: str) -> str:
    v = config.get(name)
    if not v:
        raise RuntimeError("not set")
    return v


@app.post("/api/settings/test")
def test_settings(_: dict = Depends(admin_user)):
    def gemini():
        httpx.get(f"{llm.GEMINI_BASE}/models", headers={"x-goog-api-key": _need("GEMINI_API_KEY")}, timeout=20).raise_for_status()
        return "key works"

    def groq():
        httpx.get(f"{llm.GROQ_BASE}/models", headers={"Authorization": f"Bearer {_need('GROQ_API_KEY')}"},
                  timeout=20).raise_for_status()
        return "key works"

    def openrouter():
        k = _need("OPENROUTER_API_KEY")
        r = httpx.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {k}"}, timeout=20)
        r.raise_for_status()
        d = r.json().get("data") or {}
        tier = "free tier" if d.get("is_free_tier") else "has credits"
        return f"key works ({tier}); will use {config.get('OPENROUTER_MODEL') or llm._openrouter_pick(k)}"

    def deepseek():
        k = _need("DEEPSEEK_API_KEY")
        r = httpx.get(f"{llm.DEEPSEEK_BASE}/user/balance", headers={"Authorization": f"Bearer {k}"}, timeout=20)
        r.raise_for_status()
        info = (r.json().get("balance_infos") or [{}])[0]
        bal = f"{info.get('total_balance', '?')} {info.get('currency', '')}".strip()
        return f"key works; balance {bal}" + ("" if r.json().get("is_available") else " (no balance left: top up to use it)")

    def pixabay():
        httpx.get("https://pixabay.com/api/videos/", params={"key": _need("PIXABAY_API_KEY"), "q": "ocean", "per_page": 3},
                  timeout=20).raise_for_status()
        return "key works"

    def pexels():
        httpx.get("https://api.pexels.com/videos/search", params={"query": "ocean", "per_page": 1},
                  headers={"Authorization": _need("PEXELS_API_KEY")}, timeout=20).raise_for_status()
        return "key works"

    def cloudflare():
        acc, tok = _need("CLOUDFLARE_ACCOUNT_ID"), _need("CLOUDFLARE_API_TOKEN")
        r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/meta/m2m100-1.2b",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=30,
                       json={"text": "hi", "source_lang": "en", "target_lang": "fr"})
        if r.status_code in (401, 403):
            raise RuntimeError("token rejected: it needs Account > Workers AI > Edit with your account included")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        return "token works"

    def nvidia():
        k = _need("NVIDIA_API_KEY")
        h = {"Authorization": f"Bearer {k}", "Accept": "application/json", "Content-Type": "application/json"}
        httpx.get("https://integrate.api.nvidia.com/v1/models", headers=h, timeout=20).raise_for_status()
        # empty request: 404 = Cosmos API still closed; 4xx validation error = open (nothing is generated)
        r = httpx.post(visuals.COSMOS_URL, headers=h, json={}, timeout=30)
        if r.status_code == 404:
            return "key works, but NVIDIA hasn't opened the Cosmos3-nano API yet (404). It will switch on automatically."
        if r.status_code in (400, 422):
            visuals._cosmos_state.update(available=None, checked=0)
            return "key works and the Cosmos3-nano API is open: AI clips will be used in AI/mix modes"
        raise RuntimeError(f"Cosmos endpoint: HTTP {r.status_code} {r.text[:120]}")

    checks = {"GEMINI_API_KEY": gemini, "GROQ_API_KEY": groq, "OPENROUTER_API_KEY": openrouter,
              "DEEPSEEK_API_KEY": deepseek, "NVIDIA_API_KEY": nvidia, "PIXABAY_API_KEY": pixabay,
              "PEXELS_API_KEY": pexels, "CLOUDFLARE_API_TOKEN": cloudflare, "storage": storage.check}
    out = {k: _check(fn) for k, fn in checks.items()}
    for p in publish.PLATFORMS:
        ok = publish.CONFIGURED[p]()
        out[p] = {"ok": ok, "msg": "app keys saved; users connect accounts on their channels" if ok else "app keys not set"}
    return out


@app.get("/api/admin/users")
def admin_users(_: dict = Depends(admin_user)):
    since = time.time() - 86400
    with db.session() as s:
        users = s.query(db.User).order_by(db.User.is_approved, db.User.created_at.desc()).all()  # pending first
        out = []
        for u in users:
            d = auth.user_dict(u)
            d["videos_total"] = s.query(db.Video).filter(db.Video.user_id == u.id).count()
            d["videos_today"] = s.query(db.Video).filter(db.Video.user_id == u.id, db.Video.created_at > since).count()
            d["channels"] = s.query(db.Channel).filter(db.Channel.user_id == u.id).count()
            out.append(d)
    return out


class UserPatch(BaseModel):
    is_approved: bool | None = None
    is_active: bool | None = None
    is_admin: bool | None = None
    daily_limit: int | None = Field(None, ge=-1, le=1000)   # -1 = use site default


@app.patch("/api/admin/users/{uid}")
def admin_update_user(uid: str, body: UserPatch, admin: dict = Depends(admin_user)):
    if uid == admin["id"] and (body.is_active is False or body.is_admin is False or body.is_approved is False):
        raise HTTPException(400, "You can't disable or demote yourself")
    with db.session() as s:
        u = s.get(db.User, uid)
        if not u:
            raise HTTPException(404, "No such user")
        if body.is_approved is not None:
            u.is_approved = body.is_approved
            if not body.is_approved:
                s.query(db.AuthSession).filter(db.AuthSession.user_id == uid).delete()
        if body.is_active is not None:
            u.is_active = body.is_active
            if not body.is_active:
                s.query(db.AuthSession).filter(db.AuthSession.user_id == uid).delete()
        if body.is_admin is not None:
            u.is_admin = body.is_admin
        if body.daily_limit is not None:
            u.daily_limit = None if body.daily_limit < 0 else body.daily_limit
        return auth.user_dict(u)


# ---------- OAuth (connect social accounts to a channel) ----------
def _redirect_uri(request: Request, platform: str) -> str:
    base = config.get("PUBLIC_URL").rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/oauth/{platform}/callback"


def _done_page(msg: str, ok: bool) -> HTMLResponse:
    color = "#3ecf8e" if ok else "#ff6b6b"
    safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="background:#0d0f14;color:#e8eaf0;font:16px system-ui;display:grid;place-items:center;min-height:90vh;margin:0">
<div style="max-width:520px;padding:16px"><p style="color:{color};font-weight:600">{safe}</p>
<a href="/#channels" style="color:#ffcf3f">Back to channels</a></div></body>""")


AUTH_URLS = {"youtube": publish.youtube_auth_url, "tiktok": publish.tiktok_auth_url,
             "instagram": publish.instagram_auth_url}
CALLBACKS = {"youtube": publish.youtube_callback, "tiktok": publish.tiktok_callback,
             "instagram": publish.instagram_callback}


@app.get("/oauth/{platform}/start")
def oauth_start(platform: str, channel: str, request: Request, user: dict = Depends(current_user)):
    if platform not in AUTH_URLS:
        raise HTTPException(404, "Unknown platform")
    _own_channel(channel, user)
    if not publish.CONFIGURED[platform]():
        return _done_page(f"{platform.title()} isn't set up on this site yet. Ask the admin to add the app keys.", False)
    redirect = _redirect_uri(request, platform)
    if platform in ("tiktok", "instagram") and not redirect.startswith("https://"):
        return _done_page(f"{platform.title()} only allows HTTPS sign-in addresses. Connect it from the deployed site, "
                          f"not localhost.", False)
    return RedirectResponse(AUTH_URLS[platform](channel, redirect))


@app.get("/oauth/{platform}/callback")
def oauth_callback(platform: str, request: Request, code: str = "", state: str = "", error: str = "",
                   user: dict = Depends(current_user)):
    if error:
        return _done_page(f"Sign-in was cancelled or refused: {error}", False)
    try:
        if platform not in CALLBACKS:
            raise RuntimeError("Unknown platform")
        cid = publish.take_state(state, platform)
        if not store.get_channel(cid, user["id"]):
            raise RuntimeError("That channel doesn't belong to your account")
        acc = CALLBACKS[platform](cid, code, _redirect_uri(request, platform))
    except Exception as e:
        return _done_page(str(e), False)
    return _done_page(f"✓ Connected {platform}: {acc['title']}", True)
