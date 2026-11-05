"""Video jobs: queue, background worker, database records, blob upload."""
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import auth, config, db, pipeline, publish, storage, store

LIVE: dict[str, dict] = {}                   # jobs currently queued / running / uploading
LOCK = threading.Lock()
RENDER = ThreadPoolExecutor(max_workers=1)   # one video at a time
UPLOAD = ThreadPoolExecutor(max_workers=2)   # manual approvals don't wait behind renders

AUTO_RETRIES = 3          # scheduled videos are retried automatically (network blips, busy APIs)
AUTO_RETRY_DELAY = 600    # seconds


class LimitError(Exception):
    pass


# ---------- persistence ----------
def save(job: dict) -> None:
    with db.session() as s:
        row = s.get(db.Video, job["id"])
        data = dict(job)
        if row is None:
            row = db.Video(id=job["id"], user_id=job["user_id"], created_at=job["created"], data=data,
                           status=job["status"])
            s.add(row)
        row.channel_id = job.get("channel_id")
        row.status = job["status"]
        row.title = (job.get("title") or "")[:500]
        row.finished_at = job.get("finished")
        row.blob_key = job.get("blob_key")
        row.size = job.get("size")
        row.data = data


def get(job_id: str, user_id: str | None = None) -> dict | None:
    job = LIVE.get(job_id)
    if job is None:
        with db.session() as s:
            row = s.get(db.Video, job_id)
            job = dict(row.data) if row else None
    if job and user_id and job.get("user_id") != user_id:
        return None
    return job


def list_for(user_id: str, channel_id: str | None = None, limit: int = 100) -> list[dict]:
    with db.session() as s:
        q = s.query(db.Video).filter(db.Video.user_id == user_id)
        if channel_id:
            q = q.filter(db.Video.channel_id == channel_id)
        rows = q.order_by(db.Video.created_at.desc()).limit(limit).all()
        out = [LIVE.get(r.id) or dict(r.data) for r in rows]
    return out


def public_view(job: dict) -> dict:
    """What the browser sees: no internal paths; video served through the API."""
    v = {k: val for k, val in job.items() if k not in ("blob_key",)}
    v["video"] = f"/api/videos/{job['id']}/file" if job.get("blob_key") and job["status"] == "done" else None
    return v


# ---------- worker ----------
def _log(job: dict, msg: str) -> None:
    job["log"].append(f"{time.strftime('%H:%M:%S')} {msg}")


def _worker(job: dict) -> None:
    def update(progress, step):
        job["progress"], job["step"] = int(progress), step
        _log(job, step)

    job["status"] = "running"
    workdir = config.OUTPUT / job["id"]
    try:
        pipeline.run(job, update, lambda m: _log(job, "  " + m))
        update(97, "Saving video")
        key = f"videos/{job['user_id']}/{job['id']}.mp4"
        job["size"] = storage.put_file(workdir / "final.mp4", key)
        job["blob_key"] = key
        job["status"], job["progress"], job["step"] = "done", 100, "Done"
        job["finished"] = time.time()
        save(job)
        if job.get("channel_id"):
            publish.after_render(job)
            for p, st in (job.get("publish") or {}).items():
                _log(job, f"{p}: {st.get('status')} {st.get('result') or st.get('error') or ''}".strip())
    except Exception as e:
        job["status"], job["error"] = "error", str(e)
        _log(job, traceback.format_exc(limit=3))
        _maybe_auto_retry(job)
    finally:
        job["finished"] = time.time()
        save(job)
        shutil.rmtree(workdir, ignore_errors=True)
        with LOCK:
            LIVE.pop(job["id"], None)


def _maybe_auto_retry(job: dict) -> None:
    if job.get("trigger") != "schedule" or job.get("attempts", 1) >= AUTO_RETRIES:
        return
    job["attempts"] = job.get("attempts", 1) + 1
    job["step"] = f"Failed, retrying automatically in {AUTO_RETRY_DELAY // 60} min (attempt {job['attempts']}/{AUTO_RETRIES})"
    _log(job, job["step"])

    def go():
        try:
            retry(job["id"], job["user_id"])
        except Exception:
            pass

    t = threading.Timer(AUTO_RETRY_DELAY, go)
    t.daemon = True
    t.start()


def check_limit(user: dict) -> None:
    limit = auth.daily_limit(user)
    if limit and auth.videos_today(user["id"]) >= limit:
        raise LimitError(f"Daily limit reached ({limit} videos per 24 hours). Try again later.")


def submit(user: dict, settings: dict, script: dict | None = None, topic: dict | None = None,
           channel_id: str | None = None, trigger: str = "manual") -> dict:
    ch = store.get_channel(channel_id, user["id"]) if channel_id else None
    if channel_id and not ch:
        raise KeyError(channel_id)
    check_limit(user)
    job = {
        "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        "user_id": user["id"],
        "status": "queued", "progress": 0, "step": "Queued", "log": [],
        "created": time.time(),
        "title": (script or {}).get("title") or (topic or {}).get("topic") or (f"Next video for {ch['name']}" if ch else ""),
        "script": script, "topic": topic, "settings": settings,
        "channel_id": ch["id"] if ch else None, "channel_name": ch["name"] if ch else None,
        "trigger": trigger, "error": None, "publish": {},
    }
    with LOCK:
        LIVE[job["id"]] = job
    save(job)
    RENDER.submit(_worker, job)
    return job


def retry(job_id: str, user_id: str) -> dict:
    job = get(job_id, user_id)
    if not job:
        raise KeyError(job_id)
    if job["status"] != "error":
        raise ValueError("Only failed jobs can be retried")
    job.update(status="queued", progress=0, step="Queued (retry)", error=None, publish={})
    _log(job, "--- retry ---")
    with LOCK:
        LIVE[job["id"]] = job
    save(job)
    RENDER.submit(_worker, job)
    return job


def approve(job_id: str, user_id: str, platform: str) -> dict:
    job = get(job_id, user_id)
    if not job:
        raise KeyError(job_id)
    if job["status"] != "done" or not job.get("blob_key"):
        raise ValueError("Video isn't finished yet")
    if (job.get("publish") or {}).get(platform, {}).get("status") == "uploading":
        raise ValueError("Already uploading")
    job.setdefault("publish", {})[platform] = {"status": "uploading"}
    with LOCK:
        LIVE[job["id"]] = job
    save(job)

    def run():
        try:
            publish.publish_now(job, platform)
        except Exception as e:
            job["publish"][platform] = {"status": "error", "error": str(e)}
        st = job["publish"][platform]
        _log(job, f"{platform}: {st.get('status')} {st.get('result') or st.get('error') or ''}".strip())
        save(job)
        with LOCK:
            if job["status"] == "done":
                LIVE.pop(job["id"], None)

    UPLOAD.submit(run)
    return job


def delete(job_id: str, user_id: str) -> None:
    job = get(job_id, user_id)
    if not job:
        raise KeyError(job_id)
    if job["status"] in ("queued", "running") or job_id in LIVE:
        raise ValueError("This video is still being processed")
    try:
        storage.delete(job.get("blob_key"))
    except Exception:
        pass
    with db.session() as s:
        row = s.get(db.Video, job_id)
        if row:
            s.delete(row)


def startup() -> None:
    """After a restart/redeploy: mark interrupted jobs as failed; scheduled ones retry on their own."""
    with db.session() as s:
        rows = s.query(db.Video).filter(db.Video.status.in_(("queued", "running"))).all()
        jobs = [dict(r.data) for r in rows]
    for job in jobs:
        job["status"], job["error"] = "error", "Server restarted before this finished. Click Retry."
        for p, st in (job.get("publish") or {}).items():
            if st.get("status") == "uploading":
                job["publish"][p] = {"status": "error", "error": "Server restarted during upload. Try again."}
        save(job)
        if time.time() - job.get("created", 0) < 12 * 3600:
            _maybe_auto_retry(job)
    shutil.rmtree(config.OUTPUT, ignore_errors=True)
    config.OUTPUT.mkdir(parents=True, exist_ok=True)


def cleanup(keep_days: int) -> int:
    """Delete video files older than keep_days (records are kept). 0 = keep forever."""
    if keep_days <= 0:
        return 0
    cutoff, removed = time.time() - keep_days * 86400, 0
    with db.session() as s:
        rows = s.query(db.Video).filter(db.Video.blob_key.isnot(None), db.Video.created_at < cutoff).all()
        jobs = [dict(r.data) for r in rows]
    for job in jobs:
        if any(st.get("status") in ("review", "uploading") for st in (job.get("publish") or {}).values()):
            continue  # still waiting for approval
        try:
            storage.delete(job["blob_key"])
        except Exception:
            continue
        job["blob_key"], job["expired"] = None, True
        save(job)
        removed += 1
    return removed
