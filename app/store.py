"""Channels and connected-account tokens, stored in the database and owned by a user."""
import time
import uuid

from . import db

PLATFORMS = ("youtube", "tiktok", "instagram")

DEFAULT_SETTINGS = {
    "voice": "en-US-AndrewMultilingualNeural", "rate": "+5%", "visual_mode": "mixed",
    "caption_style": "bold", "music": "random", "music_volume": 0.12,
}


def _out(c: db.Channel) -> dict:
    return {**c.data, "id": c.id, "user_id": c.user_id}


def list_channels(user_id: str | None = None) -> list[dict]:
    with db.session() as s:
        q = s.query(db.Channel)
        if user_id:
            q = q.filter(db.Channel.user_id == user_id)
        return [_out(c) for c in q.order_by(db.Channel.created_at).all()]


def get_channel(cid: str, user_id: str | None = None) -> dict | None:
    if not cid:
        return None
    with db.session() as s:
        c = s.get(db.Channel, cid)
        if not c or (user_id and c.user_id != user_id):
            return None
        return _out(c)


def update_channel(cid: str, fn) -> dict | None:
    """Apply fn(channel_dict) and save. Row-locked on Postgres."""
    with db.session() as s:
        q = s.query(db.Channel).filter(db.Channel.id == cid)
        if not db.IS_SQLITE:
            q = q.with_for_update()
        c = q.first()
        if not c:
            return None
        data = dict(c.data)
        fn(data)
        data.pop("user_id", None)
        c.data = data
        return _out(c)


def delete_channel(cid: str, user_id: str) -> None:
    with db.session() as s:
        c = s.get(db.Channel, cid)
        if c and c.user_id == user_id:
            s.query(db.OAuthToken).filter(db.OAuthToken.channel_id == cid).delete()
            s.delete(c)


def _clean_times(times) -> list[str]:
    out = []
    for t in times or []:
        try:
            hh, mm = (int(x) for x in str(t).strip().split(":"))
            if 0 <= hh < 24 and 0 <= mm < 60:
                out.append(f"{hh:02d}:{mm:02d}")
        except ValueError:
            continue
    return sorted(set(out))[:6]


def save_channel(data: dict, user_id: str, cid: str | None = None) -> dict:
    """Create or update a channel from user input, keeping server-owned fields."""
    with db.session() as s:
        row = s.get(db.Channel, cid) if cid else None
        if cid and (not row or row.user_id != user_id):
            raise KeyError(cid)
        ch = dict(row.data) if row else {
            "id": uuid.uuid4().hex[:8], "created": time.time(), "queue": [], "used": [], "runs": {},
            **{p: {"account": None} for p in PLATFORMS}}
        ch["name"] = str(data.get("name") or "My channel").strip()[:80]
        ch["niche"] = str(data.get("niche") or "").strip()[:300]
        ch["notes"] = str(data.get("notes") or "").strip()[:1500]
        ch["style"] = str(data.get("style") or "engaging, curious").strip()[:100]
        ch["language"] = str(data.get("language") or "English").strip()[:40]
        ch["duration"] = max(15, min(90, int(data.get("duration") or 45)))
        ch["settings"] = {**DEFAULT_SETTINGS, **(ch.get("settings") or {}), **(data.get("settings") or {})}

        sched_in = data.get("schedule") or {}
        old_sched = ch.get("schedule") or {}
        sched = {
            "enabled": bool(sched_in.get("enabled")),
            "times": _clean_times(sched_in.get("times")) or ["09:00"],
            "timezone": str(sched_in.get("timezone") or "UTC").strip()[:64],
        }
        # Only slots after the schedule was (re)enabled or changed may fire, so saving doesn't trigger a burst.
        changed = (sched["enabled"] and not old_sched.get("enabled")) or sched["times"] != old_sched.get("times") \
            or sched["timezone"] != old_sched.get("timezone")
        sched["since"] = time.time() if changed or "since" not in old_sched else old_sched["since"]
        ch["schedule"] = sched

        for p in PLATFORMS:
            p_in = data.get(p) or {}
            cur = dict(ch.get(p) or {"account": None})
            cur["enabled"] = bool(p_in.get("enabled"))
            cur["mode"] = "auto" if p_in.get("mode") == "auto" else "review"
            if p == "youtube":
                cur["privacy"] = p_in.get("privacy") if p_in.get("privacy") in ("public", "unlisted", "private") else "public"
            ch[p] = cur

        if "queue" in data:
            ch["queue"] = [str(t).strip()[:300] for t in data["queue"] if str(t).strip()][:200]

        ch.pop("user_id", None)
        if row:
            row.data = ch
        else:
            row = db.Channel(id=ch["id"], user_id=user_id, data=ch, created_at=ch["created"])
            s.add(row)
        s.flush()
        return _out(row)


# ---------- tokens ----------
def get_token(cid: str, platform: str) -> dict | None:
    with db.session() as s:
        t = s.get(db.OAuthToken, (cid, platform))
        return dict(t.data) if t else None


def set_token(cid: str, platform: str, token: dict) -> None:
    with db.session() as s:
        c = s.get(db.Channel, cid)
        if not c:
            raise KeyError(cid)
        t = s.get(db.OAuthToken, (cid, platform))
        if t:
            t.data, t.updated_at = dict(token), time.time()
        else:
            s.add(db.OAuthToken(channel_id=cid, platform=platform, user_id=c.user_id,
                                data=dict(token), updated_at=time.time()))


def del_token(cid: str, platform: str) -> None:
    with db.session() as s:
        s.query(db.OAuthToken).filter(db.OAuthToken.channel_id == cid,
                                      db.OAuthToken.platform == platform).delete()


def connected(cid: str) -> dict:
    with db.session() as s:
        have = {t.platform for t in s.query(db.OAuthToken).filter(db.OAuthToken.channel_id == cid).all()}
    return {p: p in have for p in PLATFORMS}
