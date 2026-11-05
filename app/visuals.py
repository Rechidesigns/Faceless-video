"""Scene visuals: stock video (Pexels, Pixabay) and AI images (Cloudflare Workers AI, FLUX schnell)."""
import base64
from pathlib import Path

import httpx

from . import config

UA = {"User-Agent": "faceless-shorts/1.0"}


def _download(url: str, out: Path) -> Path:
    with httpx.stream("GET", url, headers=UA, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for part in r.iter_bytes():
                f.write(part)
    return out


def _pick(files: list[tuple[int, int, str]]) -> str | None:
    """files = (width, height, url). Prefer portrait, close to 1080x1920, not huge."""
    if not files:
        return None

    def score(f):
        w, h, _ = f
        portrait = 0 if h >= w else 1
        too_big = 1 if max(w, h) > 2600 else 0
        return (portrait, too_big, abs(max(w, h) - 1920))

    return sorted(files, key=score)[0][2]


def pexels_video(query: str, out: Path, used: set) -> Path | None:
    key = config.get("PEXELS_API_KEY")
    if not key:
        return None
    r = httpx.get("https://api.pexels.com/videos/search", timeout=30,
                  headers={"Authorization": key, **UA},
                  params={"query": query, "orientation": "portrait", "per_page": 15})
    if r.status_code >= 400:
        return None
    videos = r.json().get("videos", [])
    videos.sort(key=lambda v: (not 4 <= v.get("duration", 0) <= 20, v.get("duration", 0)))
    for v in videos:
        vid = f"pexels:{v['id']}"
        if vid in used or v.get("duration", 0) < 3:
            continue
        files = [(f.get("width") or 0, f.get("height") or 0, f["link"])
                 for f in v.get("video_files", []) if f.get("link") and f.get("width")]
        url = _pick(files)
        if url:
            used.add(vid)
            return _download(url, out)
    return None


def pixabay_video(query: str, out: Path, used: set) -> Path | None:
    key = config.get("PIXABAY_API_KEY")
    if not key:
        return None
    r = httpx.get("https://pixabay.com/api/videos/", timeout=30, headers=UA,
                  params={"key": key, "q": query[:100], "per_page": 15, "safesearch": "true"})
    if r.status_code >= 400:
        return None
    hits = r.json().get("hits", [])
    # scenes last ~5 s, so prefer short clips: much smaller downloads
    hits.sort(key=lambda h: (not 4 <= h.get("duration", 0) <= 20, h.get("duration", 0)))
    for hit in hits:
        vid = f"pixabay:{hit['id']}"
        if vid in used or hit.get("duration", 0) < 3:
            continue
        files = [(f.get("width") or 0, f.get("height") or 0, f["url"])
                 for f in (hit.get("videos") or {}).values() if f.get("url")]
        url = _pick(files)
        if url:
            used.add(vid)
            return _download(url, out)
    return None


def cloudflare_image(prompt: str, out: Path) -> Path | None:
    acc, token = config.get("CLOUDFLARE_ACCOUNT_ID"), config.get("CLOUDFLARE_API_TOKEN")
    if not (acc and token):
        return None
    model = config.get("CLOUDFLARE_IMAGE_MODEL") or "@cf/black-forest-labs/flux-1-schnell"
    full = f"{prompt}, vertical 9:16 composition, highly detailed, cinematic, no text"
    r = httpx.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{model}",
                   headers={"Authorization": f"Bearer {token}"},
                   json={"prompt": full[:2000], "steps": 4}, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"Cloudflare image: HTTP {r.status_code} {r.text[:200]}")
    img = (r.json().get("result") or {}).get("image")
    if not img:
        return None
    out.write_bytes(base64.b64decode(img))
    return out


def fetch(index: int, scene: dict, mode: str, workdir: Path, used: set, log) -> tuple[Path | None, str]:
    """Return (path, kind) where kind is 'video', 'image' or 'none'."""
    query = scene.get("stock_query") or scene["narration"][:60]
    prompt = scene.get("image_prompt") or scene["narration"]

    def stock():
        for fn in (pexels_video, pixabay_video):
            try:
                p = fn(query, workdir / f"v_{index:02d}.mp4", used)
                if p:
                    return p, "video"
            except Exception as e:
                log(f"scene {index + 1}: {fn.__name__} failed: {e}")
        # retry with a broader query (first word only)
        short = query.split()[0] if query.split() else ""
        if short and short != query:
            for fn in (pexels_video, pixabay_video):
                try:
                    p = fn(short, workdir / f"v_{index:02d}.mp4", used)
                    if p:
                        return p, "video"
                except Exception:
                    pass
        return None

    def ai():
        try:
            p = cloudflare_image(prompt, workdir / f"i_{index:02d}.jpg")
            if p:
                return p, "image"
        except Exception as e:
            log(f"scene {index + 1}: AI image failed: {e}")
        return None

    if mode == "ai":
        order = (ai, stock)
    elif mode == "mixed":
        order = (stock, ai) if index % 2 == 0 else (ai, stock)
    else:
        order = (stock, ai)
    for fn in order:
        got = fn()
        if got:
            return got
    log(f"scene {index + 1}: no visual found, using a plain background")
    return None, "none"
