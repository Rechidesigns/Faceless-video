"""One-time import of the old single-user files (channels.json, tokens/, settings.json, output/)
into the first admin account. Files are renamed to *.imported afterwards so it never runs twice."""
import json
import traceback

from . import config, db, storage


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def import_legacy(user_id: str) -> dict:
    counts = {"channels": 0, "tokens": 0, "settings": 0, "videos": 0}
    try:
        settings = _read(config.LEGACY_SETTINGS) or {}
        if settings:
            config.save_overrides({k: v for k, v in settings.items() if k in config.EDITABLE})
            counts["settings"] = len(settings)
            config.LEGACY_SETTINGS.rename(config.LEGACY_SETTINGS.with_suffix(".json.imported"))

        channels = _read(config.LEGACY_CHANNELS) or []
        with db.session() as s:
            for ch in channels:
                if s.get(db.Channel, ch["id"]):
                    continue
                ch = {k: v for k, v in ch.items() if k != "user_id"}
                s.add(db.Channel(id=ch["id"], user_id=user_id, data=ch, created_at=ch.get("created", 0)))
                counts["channels"] += 1
        if channels:
            config.LEGACY_CHANNELS.rename(config.LEGACY_CHANNELS.with_suffix(".json.imported"))

        if config.LEGACY_TOKENS.is_dir():
            with db.session() as s:
                for f in config.LEGACY_TOKENS.glob("*.json"):
                    cid, _, platform = f.stem.partition("_")
                    tok = _read(f)
                    if tok and s.get(db.Channel, cid) and not s.get(db.OAuthToken, (cid, platform)):
                        s.add(db.OAuthToken(channel_id=cid, platform=platform, user_id=user_id, data=tok))
                        counts["tokens"] += 1
            config.LEGACY_TOKENS.rename(config.LEGACY_TOKENS.with_name("tokens.imported"))

        if config.LEGACY_OUTPUT.is_dir():
            failed = 0
            for jf in sorted(config.LEGACY_OUTPUT.glob("*/job.json")):
                job = _read(jf)
                if not job or job.get("status") != "done":
                    continue
                with db.session() as s:
                    if s.get(db.Video, job["id"]):
                        continue
                try:  # one failed upload (network blip) must not stop the rest
                    video = jf.parent / "final.mp4"
                    job["user_id"] = user_id
                    job.pop("video", None)
                    if video.exists():
                        key = f"videos/{user_id}/{job['id']}.mp4"
                        job["size"] = storage.put_file(video, key)
                        job["blob_key"] = key
                    with db.session() as s:
                        s.add(db.Video(id=job["id"], user_id=user_id, channel_id=job.get("channel_id"),
                                       status="done", title=(job.get("title") or "")[:500],
                                       created_at=job.get("created", 0), finished_at=job.get("finished"),
                                       blob_key=job.get("blob_key"), size=job.get("size"), data=job))
                    counts["videos"] += 1
                except Exception:
                    failed += 1
                    traceback.print_exc()
            counts["failed"] = failed
            if not failed:  # keep the folder so failed videos are retried on next startup
                config.LEGACY_OUTPUT.rename(config.LEGACY_OUTPUT.with_name("output.imported"))
    except Exception:
        traceback.print_exc()
    return counts


def resume_if_needed() -> None:
    """At startup: finish an import that was interrupted (e.g. upload timeouts)."""
    if not config.LEGACY_OUTPUT.is_dir():
        return
    with db.session() as s:
        admin = s.query(db.User).filter(db.User.is_admin.is_(True)).order_by(db.User.created_at).first()
        uid = admin.id if admin else None
    if uid:
        import threading
        threading.Thread(target=import_legacy, args=(uid,), daemon=True).start()
