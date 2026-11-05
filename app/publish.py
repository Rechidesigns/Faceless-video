"""Publishing: YouTube (Data API v3 resumable upload) and TikTok (Content Posting API, inbox/draft upload).

Both use OAuth. Tokens are stored per channel in DATA_DIR/tokens.
"""
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import config, storage, store

# ---------- OAuth state (short-lived, in memory) ----------
_STATES: dict[str, tuple[str, str, float]] = {}


def _new_state(platform: str, cid: str) -> str:
    now = time.time()
    for k in [k for k, v in _STATES.items() if v[2] < now]:
        _STATES.pop(k, None)
    s = secrets.token_urlsafe(24)
    _STATES[s] = (platform, cid, now + 900)
    return s


def take_state(state: str, platform: str) -> str:
    entry = _STATES.pop(state or "", None)
    if not entry or entry[0] != platform or entry[2] < time.time():
        raise RuntimeError("Sign-in link expired or invalid. Please try connecting again.")
    return entry[1]


# ======================= YouTube =======================
G_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
G_TOKEN = "https://oauth2.googleapis.com/token"
YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def youtube_configured() -> bool:
    return bool(config.get("GOOGLE_CLIENT_ID") and config.get("GOOGLE_CLIENT_SECRET"))


def youtube_auth_url(cid: str, redirect_uri: str) -> str:
    return G_AUTH + "?" + urlencode({
        "client_id": config.get("GOOGLE_CLIENT_ID"), "redirect_uri": redirect_uri,
        "response_type": "code", "scope": YT_SCOPES, "access_type": "offline",
        "prompt": "consent select_account", "include_granted_scopes": "true",
        "state": _new_state("youtube", cid),
    })


def youtube_callback(cid: str, code: str, redirect_uri: str) -> dict:
    r = httpx.post(G_TOKEN, timeout=30, data={
        "code": code, "client_id": config.get("GOOGLE_CLIENT_ID"),
        "client_secret": config.get("GOOGLE_CLIENT_SECRET"),
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    if r.status_code >= 400:
        raise RuntimeError(f"Google sign-in failed: {r.text[:300]}")
    tok = r.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
    store.set_token(cid, "youtube", tok)
    info = httpx.get("https://www.googleapis.com/youtube/v3/channels", timeout=30,
                     params={"part": "snippet", "mine": "true"},
                     headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    items = info.get("items") or []
    account = {"id": items[0]["id"], "title": items[0]["snippet"]["title"]} if items else \
        {"id": None, "title": "Google account (no YouTube channel found)"}
    store.update_channel(cid, lambda c: c["youtube"].update(account=account))
    return account


def _youtube_access(cid: str) -> str:
    tok = store.get_token(cid, "youtube")
    if not tok:
        raise RuntimeError("YouTube is not connected for this channel")
    if tok.get("expires_at", 0) - 120 < time.time():
        if not tok.get("refresh_token"):
            raise RuntimeError("YouTube sign-in expired. Reconnect YouTube for this channel.")
        r = httpx.post(G_TOKEN, timeout=30, data={
            "client_id": config.get("GOOGLE_CLIENT_ID"), "client_secret": config.get("GOOGLE_CLIENT_SECRET"),
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code >= 400:
            raise RuntimeError(f"YouTube sign-in expired ({r.text[:200]}). Reconnect YouTube.")
        new = r.json()
        tok.update(access_token=new["access_token"], expires_at=time.time() + int(new.get("expires_in", 3600)))
        store.set_token(cid, "youtube", tok)
    return tok["access_token"]


def youtube_upload(job: dict, ch: dict) -> str:
    access = _youtube_access(ch["id"])
    script = job["script"]
    tags = [h.lstrip("#") for h in script.get("hashtags", [])]
    title = script["title"].strip()[:90]
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"
    description = "\n\n".join(x for x in [script.get("description", ""), " ".join(script.get("hashtags", []))] if x)
    uses_ai = job["settings"].get("visual_mode") in ("ai", "mixed")
    body = {
        "snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:15],
                    "categoryId": "22"},
        "status": {"privacyStatus": ch["youtube"].get("privacy", "public"),
                   "selfDeclaredMadeForKids": False, "containsSyntheticMedia": uses_ai},
    }
    with storage.local_copy(job) as path:
        blob = path.read_bytes()
    size = len(blob)
    r = httpx.post("https://www.googleapis.com/upload/youtube/v3/videos", timeout=60,
                   params={"uploadType": "resumable", "part": "snippet,status"},
                   headers={"Authorization": f"Bearer {access}", "X-Upload-Content-Type": "video/mp4",
                            "X-Upload-Content-Length": str(size)},
                   json=body)
    if r.status_code >= 400 or "location" not in r.headers:
        raise RuntimeError(f"YouTube upload start failed: HTTP {r.status_code} {r.text[:300]}")
    r2 = httpx.put(r.headers["location"], content=blob, timeout=900,
                   headers={"Content-Type": "video/mp4"})
    if r2.status_code >= 400:
        raise RuntimeError(f"YouTube upload failed: HTTP {r2.status_code} {r2.text[:300]}")
    return f"https://youtube.com/shorts/{r2.json()['id']}"


# ======================= TikTok =======================
TT_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TT_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
TT_API = "https://open.tiktokapis.com/v2"


def tiktok_configured() -> bool:
    return bool(config.get("TIKTOK_CLIENT_KEY") and config.get("TIKTOK_CLIENT_SECRET"))


def tiktok_auth_url(cid: str, redirect_uri: str) -> str:
    return TT_AUTH + "?" + urlencode({
        "client_key": config.get("TIKTOK_CLIENT_KEY"), "response_type": "code",
        "scope": "user.info.basic,video.upload", "redirect_uri": redirect_uri,
        "state": _new_state("tiktok", cid),
    })


def _tt_token_request(data: dict) -> dict:
    r = httpx.post(TT_TOKEN, timeout=30, data={
        "client_key": config.get("TIKTOK_CLIENT_KEY"),
        "client_secret": config.get("TIKTOK_CLIENT_SECRET"), **data},
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = r.json() if r.content else {}
    if r.status_code >= 400 or "access_token" not in tok:
        raise RuntimeError(f"TikTok sign-in failed: {tok.get('error_description') or tok.get('error') or r.text[:300]}")
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 86400))
    return tok


def tiktok_callback(cid: str, code: str, redirect_uri: str) -> dict:
    tok = _tt_token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
    store.set_token(cid, "tiktok", tok)
    info = httpx.get(f"{TT_API}/user/info/", params={"fields": "open_id,display_name"}, timeout=30,
                     headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    user = (info.get("data") or {}).get("user") or {}
    account = {"id": tok.get("open_id"), "title": user.get("display_name") or "TikTok account"}
    store.update_channel(cid, lambda c: c["tiktok"].update(account=account))
    return account


def _tiktok_access(cid: str) -> str:
    tok = store.get_token(cid, "tiktok")
    if not tok:
        raise RuntimeError("TikTok is not connected for this channel")
    if tok.get("expires_at", 0) - 300 < time.time():
        new = _tt_token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        store.set_token(cid, "tiktok", new)
        return new["access_token"]
    return tok["access_token"]


def tiktok_upload(job: dict, ch: dict) -> str:
    """Send the video to the TikTok app's inbox as a draft; the user adds the caption and posts."""
    access = _tiktok_access(ch["id"])
    with storage.local_copy(job) as path:
        blob = path.read_bytes()
    size = len(blob)
    mb = 1024 * 1024
    chunk = size if size <= 64 * mb else 10 * mb
    count = max(1, size // chunk)
    r = httpx.post(f"{TT_API}/post/publish/inbox/video/init/", timeout=60,
                   headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json; charset=UTF-8"},
                   json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                         "chunk_size": chunk, "total_chunk_count": count}})
    data = r.json() if r.content else {}
    err = data.get("error") or {}
    if r.status_code >= 400 or err.get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok upload start failed: {err.get('message') or r.text[:300]}")
    upload_url = data["data"]["upload_url"]
    for i in range(count):
        start = i * chunk
        end = size if i == count - 1 else start + chunk  # last chunk takes the remainder
        part = blob[start:end]
        up = httpx.put(upload_url, content=part, timeout=600, headers={
            "Content-Type": "video/mp4", "Content-Length": str(len(part)),
            "Content-Range": f"bytes {start}-{end - 1}/{size}"})
        if up.status_code >= 400:
            raise RuntimeError(f"TikTok upload failed: HTTP {up.status_code} {up.text[:200]}")
    return "Sent to your TikTok inbox. Open TikTok > notifications to finish posting."


# ======================= Instagram (Instagram API with Instagram Login) =======================
IG_AUTH = "https://www.instagram.com/oauth/authorize"
IG_TOKEN = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com"
IG_SCOPES = "instagram_business_basic,instagram_business_content_publish"


def _ig_ver() -> str:
    return config.get("INSTAGRAM_API_VERSION") or "v24.0"


def instagram_configured() -> bool:
    return bool(config.get("INSTAGRAM_APP_ID") and config.get("INSTAGRAM_APP_SECRET"))


def instagram_auth_url(cid: str, redirect_uri: str) -> str:
    return IG_AUTH + "?" + urlencode({
        "client_id": config.get("INSTAGRAM_APP_ID"), "redirect_uri": redirect_uri,
        "response_type": "code", "scope": IG_SCOPES, "state": _new_state("instagram", cid),
    })


def instagram_callback(cid: str, code: str, redirect_uri: str) -> dict:
    code = code.split("#")[0]
    r = httpx.post(IG_TOKEN, timeout=30, data={
        "client_id": config.get("INSTAGRAM_APP_ID"), "client_secret": config.get("INSTAGRAM_APP_SECRET"),
        "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code})
    j = r.json() if r.content else {}
    if isinstance(j.get("data"), list) and j["data"]:
        j = j["data"][0]
    if r.status_code >= 400 or "access_token" not in j:
        raise RuntimeError(f"Instagram sign-in failed: {j.get('error_message') or r.text[:300]}")
    # exchange the 1-hour token for a 60-day one
    r2 = httpx.get(f"{IG_GRAPH}/access_token", timeout=30, params={
        "grant_type": "ig_exchange_token", "client_secret": config.get("INSTAGRAM_APP_SECRET"),
        "access_token": j["access_token"]})
    long = r2.json()
    if r2.status_code >= 400 or "access_token" not in long:
        raise RuntimeError(f"Instagram token exchange failed: {r2.text[:300]}")
    now = time.time()
    tok = {"access_token": long["access_token"], "user_id": str(j.get("user_id", "")),
           "expires_at": now + int(long.get("expires_in", 5184000)), "obtained": now}
    me = httpx.get(f"{IG_GRAPH}/{_ig_ver()}/me", timeout=30,
                   params={"fields": "user_id,username,account_type", "access_token": tok["access_token"]}).json()
    tok["user_id"] = str(me.get("user_id") or tok["user_id"])
    store.set_token(cid, "instagram", tok)
    account = {"id": tok["user_id"], "title": "@" + me["username"] if me.get("username") else "Instagram account"}
    store.update_channel(cid, lambda c: c.setdefault("instagram", {}).update(account=account))
    return account


def _instagram_access(cid: str) -> dict:
    tok = store.get_token(cid, "instagram")
    if not tok:
        raise RuntimeError("Instagram is not connected for this channel")
    now = time.time()
    if tok.get("expires_at", 0) < now:
        raise RuntimeError("Instagram sign-in expired. Reconnect Instagram for this channel.")
    # long-lived tokens can be refreshed once they're 24h old; refresh when <10 days remain
    if tok["expires_at"] - now < 10 * 86400 and now - tok.get("obtained", 0) > 86400:
        r = httpx.get(f"{IG_GRAPH}/refresh_access_token", timeout=30,
                      params={"grant_type": "ig_refresh_token", "access_token": tok["access_token"]})
        if r.status_code < 400 and "access_token" in r.json():
            new = r.json()
            tok.update(access_token=new["access_token"], obtained=now,
                       expires_at=now + int(new.get("expires_in", 5184000)))
            store.set_token(cid, "instagram", tok)
    return tok


def refresh_instagram_tokens() -> None:
    for ch in store.list_channels():
        if store.get_token(ch["id"], "instagram"):
            try:
                _instagram_access(ch["id"])
            except Exception:
                pass


# ---- temporary public links (Instagram downloads the video from a URL) ----
_seen_base: list[str] = []


def remember_base(url: str) -> None:
    if url.startswith("https://") and not _seen_base[:1] == [url]:
        _seen_base[:] = [url]


def public_base() -> str:
    return (config.get("PUBLIC_URL") or (_seen_base[0] if _seen_base else "")).rstrip("/")


def sign(job_id: str, exp: int) -> str:
    import hashlib
    import hmac
    return hmac.new(config.secret_key(), f"{job_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def signed_video_url(job: dict, ttl: int = 6 * 3600) -> str:
    if storage.remote() and job.get("blob_key"):
        return storage.presigned_url(job["blob_key"], ttl)  # Instagram fetches straight from the bucket
    job_id = job["id"]
    base = public_base()
    if not base.startswith("https://"):
        raise RuntimeError("Instagram needs this app on a public HTTPS address. Deploy it (Railway) "
                           "and set PUBLIC_URL in Settings.")
    exp = int(time.time()) + ttl
    return f"{base}/pub/{job_id}/{exp}/{sign(job_id, exp)}/video.mp4"


def instagram_upload(job: dict, ch: dict) -> str:
    tok = _instagram_access(ch["id"])
    ver, access, uid = _ig_ver(), tok["access_token"], tok["user_id"]
    script = job["script"]
    caption = "\n\n".join(x for x in [script["title"], script.get("description", ""),
                                       " ".join(script.get("hashtags", [])[:25])] if x)[:2150]
    r = httpx.post(f"{IG_GRAPH}/{ver}/{uid}/media", timeout=60, data={
        "media_type": "REELS", "video_url": signed_video_url(job), "caption": caption,
        "share_to_feed": "true", "access_token": access})
    if r.status_code >= 400 or "id" not in r.json():
        raise RuntimeError(f"Instagram upload start failed: {r.text[:300]}")
    container = r.json()["id"]
    # Instagram fetches and processes the video; wait for it (up to ~10 minutes)
    for _ in range(60):
        time.sleep(10)
        st = httpx.get(f"{IG_GRAPH}/{ver}/{container}", timeout=30,
                       params={"fields": "status_code,status", "access_token": access}).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {st.get('status') or code}")
    else:
        raise RuntimeError("Instagram is still processing after 10 minutes. Try again later.")
    p = httpx.post(f"{IG_GRAPH}/{ver}/{uid}/media_publish", timeout=60,
                   data={"creation_id": container, "access_token": access})
    if p.status_code >= 400 or "id" not in p.json():
        raise RuntimeError(f"Instagram publish failed: {p.text[:300]}")
    media_id = p.json()["id"]
    link = httpx.get(f"{IG_GRAPH}/{ver}/{media_id}", timeout=30,
                     params={"fields": "permalink", "access_token": access}).json().get("permalink")
    return link or f"Posted to Instagram (media id {media_id})"


# ======================= after render / approve =======================
PLATFORMS = ("youtube", "tiktok", "instagram")
UPLOADERS = {"youtube": youtube_upload, "tiktok": tiktok_upload, "instagram": instagram_upload}
CONFIGURED = {"youtube": youtube_configured, "tiktok": tiktok_configured, "instagram": instagram_configured}


def _upload(job: dict, ch: dict, platform: str) -> None:
    job.setdefault("publish", {})[platform] = {"status": "uploading"}
    try:
        result = UPLOADERS[platform](job, ch)
        job["publish"][platform] = {"status": "done", "result": result, "at": time.time()}
    except Exception as e:
        job["publish"][platform] = {"status": "error", "error": str(e)}


def after_render(job: dict) -> None:
    ch = store.get_channel(job.get("channel_id") or "")
    if not ch:
        return
    job["publish"] = {}
    for p in PLATFORMS:
        cfg = ch.get(p) or {}
        if not cfg.get("enabled"):
            continue
        if not store.get_token(ch["id"], p):
            job["publish"][p] = {"status": "not_connected"}
        elif cfg.get("mode") == "auto":
            _upload(job, ch, p)
        else:
            job["publish"][p] = {"status": "review"}


def publish_now(job: dict, platform: str) -> None:
    ch = store.get_channel(job.get("channel_id") or "")
    if not ch:
        raise RuntimeError("This video isn't linked to a channel")
    if not store.get_token(ch["id"], platform):
        raise RuntimeError(f"Connect {platform} for channel '{ch['name']}' first")
    _upload(job, ch, platform)
