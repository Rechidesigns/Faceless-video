"""Scene visuals.

Each scene is a fast-cut sequence of 1-3 shots. Sources, best first:
  * NVIDIA Cosmos3-nano AI video clips (only when its API is live; capped per video)
  * stock video of real people (Pixabay, Pexels)
  * AI still images (Cloudflare Workers AI, FLUX) with a Ken Burns move
  * a plain background (last resort)
"""
import base64
import random
import time
from pathlib import Path

import httpx

from . import config

UA = {"User-Agent": "faceless-shorts/1.0"}
SHOT_SEP = "|"


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


def _short_first(items: list[dict]) -> list[dict]:
    # shots last 2-3 s, so prefer short clips (smaller downloads), keep some randomness for variety
    random.shuffle(items)
    return sorted(items, key=lambda h: not 4 <= h.get("duration", 0) <= 25)


# ---- relevance: stock libraries happily return loosely related clips; score them against the query ----
STOP = {"a", "an", "the", "of", "on", "in", "at", "to", "and", "with", "for", "his", "her", "their", "is", "up",
        "out", "into", "from", "by", "morning", "night", "evening", "sunrise", "sunset", "day", "today", "dawn"}
BANNED = ("green screen", "greenscreen", "chroma", "subscribe", "like button", "follow button", "overlay",
          "intro", "logo", "countdown", "transition", "lower third", "template", "animated text", "alpha channel",
          "bell icon", "notification")


def _stem(w: str) -> str:
    for suf in ("ing", "es", "s", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def query_words(q: str) -> list[str]:
    return [_stem(w) for w in q.lower().replace("-", " ").split() if w not in STOP and len(w) > 1]


def relevance(query: str, tags: str) -> int:
    """How many query words appear in the clip's tags/description. -1 = banned (green screen, subscribe…)."""
    t = tags.lower()
    if any(b in t for b in BANNED):
        return -1
    tag_words = {_stem(w) for w in t.replace(",", " ").replace("-", " ").split()}
    # also match multi-word tags written as one word: "push ups" ~ "pushups", "sun rise" ~ "sunrise"
    tag_words |= {_stem(p.replace(" ", "").replace("-", "")) for p in t.split(",") if p.strip()}
    return sum(1 for w in query_words(query) if w in tag_words or any(tw.startswith(w) for tw in tag_words))


def _best(candidates: list[tuple[int, dict]], need: int) -> dict | None:
    """candidates = (relevance, item). Highest relevance first, short clips preferred among equals."""
    ok = [(s, c) for s, c in candidates if s >= need]
    if not ok:
        return None
    top = max(s for s, _ in ok)
    return _short_first([c for s, c in ok if s == top])[0]


def pexels_video(query: str, out: Path, used: set, need: int = 1) -> Path | None:
    key = config.get("PEXELS_API_KEY")
    if not key:
        return None
    r = httpx.get("https://api.pexels.com/videos/search", timeout=30,
                  headers={"Authorization": key, **UA},
                  params={"query": query, "orientation": "portrait", "per_page": 20})
    if r.status_code >= 400:
        return None
    cands = []
    for v in r.json().get("videos", []):
        if f"pexels:{v['id']}" in used or v.get("duration", 0) < 3:
            continue
        slug = (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1]   # e.g. "woman-reading-a-book-3209211"
        cands.append((relevance(query, slug.replace("-", " ")), v))
    v = _best(cands, need)
    if not v:
        return None
    files = [(f.get("width") or 0, f.get("height") or 0, f["link"])
             for f in v.get("video_files", []) if f.get("link") and f.get("width")]
    url = _pick(files)
    if not url:
        return None
    used.add(f"pexels:{v['id']}")
    return _download(url, out)


def pixabay_video(query: str, out: Path, used: set, need: int = 1) -> Path | None:
    key = config.get("PIXABAY_API_KEY")
    if not key:
        return None
    r = httpx.get("https://pixabay.com/api/videos/", timeout=30, headers=UA,
                  params={"key": key, "q": query[:100], "per_page": 30, "safesearch": "true",
                          "video_type": "film"})
    if r.status_code >= 400:
        return None
    cands = [(relevance(query, h.get("tags", "")), h) for h in r.json().get("hits", [])
             if f"pixabay:{h['id']}" not in used and h.get("duration", 0) >= 3]
    hit = _best(cands, need)
    if not hit:
        return None
    files = [(f.get("width") or 0, f.get("height") or 0, f["url"])
             for f in (hit.get("videos") or {}).values() if f.get("url")]
    url = _pick(files)
    if not url:
        return None
    used.add(f"pixabay:{hit['id']}")
    return _download(url, out)


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


# ---------------- NVIDIA Cosmos3-nano (AI video clips) ----------------
COSMOS_URL = "https://ai.api.nvidia.com/v1/cosmos/nvidia/cosmos3-nano"
_cosmos_state = {"available": None, "checked": 0.0, "detail": "not checked yet"}
COSMOS_RECHECK = 6 * 3600   # while the endpoint is closed, look again at most every 6 hours


def cosmos_enabled() -> bool:
    return bool(config.get("NVIDIA_API_KEY")) and config.flag("COSMOS_ENABLED", True)


def cosmos_status() -> dict:
    return {"enabled": cosmos_enabled(), **_cosmos_state}


def _cosmos_closed(detail: str) -> None:
    _cosmos_state.update(available=False, checked=time.time(), detail=detail)


def cosmos_clip(prompt: str, out: Path, seconds: float) -> Path | None:
    """Generate a short vertical AI video clip. Returns None (quickly) while the API isn't open."""
    if not cosmos_enabled():
        return None
    if _cosmos_state["available"] is False and time.time() - _cosmos_state["checked"] < COSMOS_RECHECK:
        return None
    fps = 24
    frames = max(25, min(121, int(seconds * fps) // 4 * 4 + 1))   # ~1-5 s, frame count of the form 4n+1
    body = {"model_mode": "text2video", "resolution": "480_9_16", "num_frames": frames, "fps": fps,
            "num_inference_steps": 35, "seed": random.randint(1, 10**6),
            "prompt": f"{prompt}. Realistic, natural human motion, cinematic, no text."[:1500]}
    r = httpx.post(COSMOS_URL, json=body, timeout=900,
                   headers={"Authorization": f"Bearer {config.get('NVIDIA_API_KEY')}",
                            "Accept": "application/json", "Content-Type": "application/json"})
    if r.status_code in (401, 403, 404):
        _cosmos_closed(f"API not available (HTTP {r.status_code})")
        return None
    if r.status_code == 422:
        raise RuntimeError(f"Cosmos refused the prompt: {r.text[:200]}")
    if r.status_code >= 400:
        raise RuntimeError(f"Cosmos: HTTP {r.status_code} {r.text[:200]}")
    b64 = (r.json() or {}).get("b64_video")
    if not b64:
        raise RuntimeError("Cosmos returned no video")
    out.write_bytes(base64.b64decode(b64))
    _cosmos_state.update(available=True, checked=time.time(), detail="working")
    return out


# ---------------- per-scene shot planning ----------------
def shot_queries(scene: dict) -> list[str]:
    raw = scene.get("stock_query") or scene["narration"][:60]
    shots = [s.strip() for s in raw.split(SHOT_SEP) if s.strip()]
    return shots or [scene["narration"][:60]]


def fetch_scene(index: int, scene: dict, mode: str, workdir: Path, used: set, log, n_shots: int,
                shot_seconds: float, cosmos_budget: list) -> list[tuple[Path | None, str]]:
    """Return up to n_shots (path, kind) items for one scene. kind: 'video', 'image' or 'none'."""
    queries = shot_queries(scene)
    prompt = scene.get("image_prompt") or scene["narration"]
    shots: list[tuple[Path, str]] = []

    def stock(q: str, k: int):
        # 1) full query, clip must match 2+ of its words (e.g. "man" + "pushup")
        # 2) without the last word (often a place), still 2+ matches
        # never fall back to a single vague word: an unrelated clip is worse than an AI image
        words = q.split()
        need = min(2, len(query_words(q)))
        attempts = [(q, need)]
        if len(words) >= 3:
            shorter = " ".join(words[:-1])
            attempts.append((shorter, min(2, len(query_words(shorter)))))
        for attempt, n in attempts:
            if n == 0:
                continue
            for fn in (pixabay_video, pexels_video):
                try:
                    p = fn(attempt, workdir / f"v_{index:02d}_{k}.mp4", used, n)
                    if p:
                        return p
                except Exception as e:
                    log(f"scene {index + 1}: {fn.__name__} '{attempt}' failed: {e}")
        log(f"scene {index + 1}: no relevant footage for '{q}'")
        return None

    def ai_image():
        try:
            return cloudflare_image(prompt, workdir / f"i_{index:02d}.jpg")
        except Exception as e:
            log(f"scene {index + 1}: AI image failed: {e}")
            return None

    # 1) Cosmos AI clip (ai/mixed modes; a few per video; silently skipped while NVIDIA's API is closed)
    if mode in ("ai", "mixed") and cosmos_budget[0] > 0:
        try:
            p = cosmos_clip(prompt, workdir / f"c_{index:02d}.mp4", shot_seconds * n_shots)
            if p:
                cosmos_budget[0] -= 1
                log(f"scene {index + 1}: Cosmos AI clip")
                return [(p, "video")]
        except Exception as e:
            log(f"scene {index + 1}: Cosmos failed: {e}")

    # 2) AI still image: "ai" mode always; "mixed" roughly one scene in four (never the hook)
    if mode == "ai" or (mode == "mixed" and index > 0 and index % 4 == 3):
        p = ai_image()
        if p:
            return [(p, "image")]

    # 3) people-first stock footage, one clip per planned shot. If a shot's query finds nothing relevant,
    #    fill the slot with another (different) clip from the scene's other queries so the cuts stay fast.
    failed: set[str] = set()
    for k in range(n_shots):
        order = queries[k % len(queries):] + queries[:k % len(queries)]
        for q in order:
            if q in failed:
                continue
            p = stock(q, k)
            if p:
                shots.append((p, "video"))
                break
            failed.add(q)
    if shots:
        return shots

    # 4) fallbacks
    p = ai_image()
    if p:
        return [(p, "image")]
    log(f"scene {index + 1}: no visual found, using a plain background")
    return [(None, "none")]
