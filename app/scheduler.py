"""Daily automation: fires each channel's scheduled time slots and queues a video job."""
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import auth, config, db, jobs, publish, store

_started = False
_last_cleanup = 0.0


def _tz(name: str):
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return timezone.utc


def _slots(ch: dict, day) -> list[tuple[str, datetime]]:
    tz = _tz(ch["schedule"].get("timezone"))
    out = []
    for t in ch["schedule"].get("times", []):
        hh, mm = (int(x) for x in t.split(":"))
        slot = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        out.append((f"{day.isoformat()} {t}", slot))
    return out


def next_run(ch: dict) -> float | None:
    sch = ch.get("schedule") or {}
    if not sch.get("enabled") or not sch.get("times"):
        return None
    now = datetime.now(_tz(sch.get("timezone")))
    for offset in (0, 1):
        for key, slot in _slots(ch, (now + timedelta(days=offset)).date()):
            if key not in ch.get("runs", {}) and slot.timestamp() >= max(sch.get("since", 0), time.time() - 1):
                return slot.timestamp()
    return None


def _owner(user_id: str) -> dict | None:
    with db.session() as s:
        u = s.get(db.User, user_id)
        return auth.user_dict(u) if u and u.is_active and u.is_approved else None


def run_channel(cid: str, trigger: str = "manual", slot_key: str | None = None,
                user: dict | None = None) -> dict | None:
    ch = store.get_channel(cid, user["id"] if user else None)
    if not ch:
        raise KeyError(cid)
    owner = user or _owner(ch["user_id"])
    result: str
    job = None
    if not owner:
        result = "skipped: account disabled"
    else:
        topic = {"topic": None, "duration": ch["duration"], "style": ch["style"],
                 "language": ch["language"], "niche": ch["niche"], "notes": ch.get("notes", "")}
        try:
            job = jobs.submit(owner, dict(ch["settings"]), topic=topic, channel_id=cid, trigger=trigger)
            result = job["id"]
        except jobs.LimitError as e:
            if not slot_key:
                raise
            result = f"skipped: {e}"
    if slot_key:
        def mark(c):
            c.setdefault("runs", {})[slot_key] = result
            c["runs"] = dict(sorted(c["runs"].items())[-60:])
        store.update_channel(cid, mark)
    return job


def tick() -> None:
    global _last_cleanup
    for ch in store.list_channels():
        sch = ch.get("schedule") or {}
        if not sch.get("enabled"):
            continue
        now = datetime.now(_tz(sch.get("timezone")))
        for key, slot in _slots(ch, now.date()):
            # due, not yet run, scheduled after the schedule was saved, and not more than 12h late
            if (now >= slot and key not in ch.get("runs", {})
                    and slot.timestamp() >= sch.get("since", 0) and now - slot < timedelta(hours=12)):
                run_channel(ch["id"], trigger="schedule", slot_key=key)
    if time.time() - _last_cleanup > 6 * 3600:
        _last_cleanup = time.time()
        try:
            jobs.cleanup(int(config.get("KEEP_VIDEOS_DAYS", "0") or 0))
        except ValueError:
            pass
        publish.refresh_instagram_tokens()  # 60-day tokens; keep them alive


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def start() -> None:
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="scheduler").start()
