"""Video file storage: S3-compatible bucket (Railway Bucket, Cloudflare R2, AWS S3) or a local folder.

Bucket credentials are read from env, in this order:
  S3_ENDPOINT / S3_BUCKET / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_REGION
  AWS_ENDPOINT_URL / AWS_S3_BUCKET_NAME / AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION
  ENDPOINT / BUCKET / ACCESS_KEY_ID / SECRET_ACCESS_KEY / REGION   (Railway Bucket variable names)
"""
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import config

LOCAL_ROOT = config.DATA / "blobs"
_client: dict = {}


def _pick(*names: str) -> str:
    return next((config.env(n) for n in names if config.env(n)), "")


def _cfg() -> dict:
    return {
        "endpoint": _pick("S3_ENDPOINT", "AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL", "ENDPOINT"),
        "bucket": _pick("S3_BUCKET", "AWS_S3_BUCKET_NAME", "BUCKET"),
        "key": _pick("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", "ACCESS_KEY_ID"),
        "secret": _pick("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "SECRET_ACCESS_KEY"),
        "region": _pick("S3_REGION", "AWS_DEFAULT_REGION", "AWS_REGION", "REGION") or "auto",
        "style": config.env("S3_ADDRESSING_STYLE") or "virtual",
    }


def remote() -> bool:
    c = _cfg()
    return bool(c["bucket"] and c["key"] and c["secret"])


def describe() -> str:
    c = _cfg()
    return f"bucket '{c['bucket']}'" if remote() else "local folder"


def _s3():
    c = _cfg()
    sig = tuple(c.values())
    if _client.get("sig") != sig:
        import boto3
        from botocore.config import Config
        _client["s3"] = boto3.client(
            "s3", endpoint_url=c["endpoint"] or None, region_name=c["region"],
            aws_access_key_id=c["key"], aws_secret_access_key=c["secret"],
            config=Config(signature_version="s3v4", s3={"addressing_style": c["style"]},
                          retries={"max_attempts": 5, "mode": "standard"}))
        _client["sig"] = sig
    return _client["s3"], c["bucket"]


def _local(key: str) -> Path:
    p = (LOCAL_ROOT / key).resolve()
    if LOCAL_ROOT.resolve() not in p.parents:
        raise ValueError("bad key")
    return p


def put_file(path: Path, key: str, content_type: str = "video/mp4") -> int:
    size = path.stat().st_size
    if remote():
        s3, bucket = _s3()
        s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})
    else:
        dest = _local(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    return size


def presigned_url(key: str, ttl: int = 3600, download_name: str | None = None) -> str | None:
    """Temporary URL for a remote object, or None when using local storage."""
    if not remote():
        return None
    s3, bucket = _s3()
    params = {"Bucket": bucket, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
    return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)


def local_path(key: str) -> Path:
    return _local(key)


def delete(key: str) -> None:
    if not key:
        return
    if remote():
        s3, bucket = _s3()
        s3.delete_object(Bucket=bucket, Key=key)
    else:
        _local(key).unlink(missing_ok=True)


@contextmanager
def local_copy(job: dict):
    """Yield a local path to the job's video: the render folder copy, the local blob, or a temp download."""
    work = config.OUTPUT / job["id"] / "final.mp4"
    if work.exists():
        yield work
        return
    key = job.get("blob_key")
    if not key:
        raise RuntimeError("This video file no longer exists")
    if not remote():
        yield _local(key)
        return
    tmp = Path(tempfile.mkdtemp()) / "video.mp4"
    try:
        s3, bucket = _s3()
        s3.download_file(bucket, key, str(tmp))
        yield tmp
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def check() -> str:
    """Round-trip test for the Settings page."""
    if not remote():
        return f"using local folder {LOCAL_ROOT} (set bucket variables to use a bucket)"
    s3, bucket = _s3()
    key = "healthcheck/ping.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=b"ok")
    s3.delete_object(Bucket=bucket, Key=key)
    return f"bucket '{bucket}' works (write + delete)"
