"""Script and topic writing: Gemini free tier first, Groq as fallback."""
import json
import re
import time

import httpx

from . import config

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GROQ_BASE = "https://api.groq.com/openai/v1"

SCRIPT_SYSTEM = """You write scripts for faceless short-form videos (TikTok, YouTube Shorts, Instagram Reels).
Rules:
- The first sentence is a scroll-stopping hook.
- Short, punchy, spoken-language sentences. No emojis, no stage directions, no hashtags inside narration.
- Each scene's narration is 1-2 sentences.
- End with a satisfying payoff or a short call to action.
- Be factually careful; do not invent statistics.
- shots: the stock-footage plan for the scene. Give 2 shots per scene (3 for the first scene, which is the hook).
  Each shot is a 2-4 word English search query for real filmed footage of a concrete moment.
  STRONGLY prefer real people in action or showing emotion: e.g. "man sprinting stairs", "woman laughing phone",
  "scientist looking microscope", "crowd cheering stadium", "chef chopping vegetables", "boy surprised face".
  When people don't fit, use vivid moving footage ("waves crashing rocks", "city traffic"). Never abstract words.
  Write them the way stock libraries tag clips: WHO + ACTION (+ PLACE), common words, e.g. "woman jogging park",
  "man writing notebook", "family eating dinner". No time-of-day words (morning, night, sunrise), no brand names,
  and never screen/UI elements (no "follow button", "subscribe", "like", "app screen", "text").
  Consecutive shots should feel like a fast-cut edit: vary angle and subject (close-up, wide, reaction).
- image_prompt: a vivid English prompt for an AI image/video generator showing the scene with people in action:
  subject, action, setting, lighting, mood, cinematic, vertical composition. No text, letters, words or logos.
- title: under 70 characters, curiosity-driven, no hashtags.
Return ONLY a JSON object with exactly this shape:
{"title": "...", "scenes": [{"narration": "...", "shots": ["...", "..."], "image_prompt": "..."}], "description": "...", "hashtags": ["#tag"]}"""

SHOT_SEP = " | "  # shots are stored in scene["stock_query"] joined by this, so the editor shows them as one field

TOPICS_SYSTEM = """You plan content for a faceless short-form video channel (TikTok / YouTube Shorts).
Generate specific, curiosity-driven video topics that fit the channel's niche.
Rules:
- Each topic is one concrete idea a 30-60 second video can fully cover, max 14 words.
- Mix formats: surprising facts, myths vs truth, mini stories, "what if", comparisons, little-known history.
- No generic categories, no clickbait lies, no duplicates or near-duplicates of the topics listed as already used.
Return ONLY JSON: {"topics": ["...", "..."]}"""

_gemini_list: list[str] = []


def build_prompt(topic: str, duration: int, style: str, language: str,
                 niche: str = "", notes: str = "") -> str:
    words = int(duration * 2.5)
    scenes = max(3, round(duration / 5))
    channel = ""
    if niche:
        channel = f"Channel niche: {niche}\n"
    if notes:
        channel += f"Channel guidelines: {notes}\n"
    return (
        f"{channel}"
        f"Topic: {topic}\n"
        f"Tone/style: {style}\n"
        f"Narration language: {language}. (stock_query and image_prompt are always English.)\n"
        f"Length: about {words} words of narration in total, split into about {scenes} scenes.\n"
        f"Write the script now."
    )


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model did not return JSON")
    return json.loads(text[start : end + 1])


def _normalize(data: dict, topic: str) -> dict:
    scenes = []
    for sc in data.get("scenes") or []:
        narration = str(sc.get("narration", "")).strip()
        if not narration:
            continue
        shots = sc.get("shots") or []
        if isinstance(shots, str):
            shots = shots.split("|")
        shots = [str(s).strip() for s in shots if str(s).strip()][:3]
        if not shots:
            shots = [str(sc.get("stock_query") or topic).strip()]
        scenes.append({
            "narration": narration,
            "stock_query": SHOT_SEP.join(shots),
            "image_prompt": str(sc.get("image_prompt") or narration).strip(),
        })
    if not scenes:
        raise ValueError("model returned no scenes")
    tags = data.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    tags = ["#" + str(t).lstrip("#").replace(" ", "") for t in tags if str(t).strip("# ")]
    return {
        "title": str(data.get("title") or topic).strip(),
        "scenes": scenes,
        "description": str(data.get("description") or "").strip(),
        "hashtags": tags[:12],
    }


# ---------- Gemini ----------

def _gemini_models(key: str) -> list[str]:
    """Candidate models, best first: newest stable Flash, then older/preview Flash, then Flash-Lite."""
    wanted = config.get("GEMINI_MODEL")
    if wanted:
        return [wanted]
    if _gemini_list:
        return _gemini_list
    ranked = []
    try:
        r = httpx.get(f"{GEMINI_BASE}/models", params={"pageSize": 1000},
                      headers={"x-goog-api-key": key}, timeout=30)
        r.raise_for_status()
        for m in r.json().get("models", []):
            name = m["name"].split("/", 1)[-1].lower()
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if not name.startswith("gemini-") or "flash" not in name:
                continue
            if any(b in name for b in ("image", "tts", "live", "audio", "embed", "exp", "computer", "robotics")):
                continue
            ver = re.match(r"gemini-(\d+(?:\.\d+)?)", name)
            rank = (0 if "lite" in name else 1, 0 if "preview" in name else 1,
                    float(ver.group(1)) if ver else 0.0, -len(name))
            ranked.append((rank, name))
    except httpx.HTTPError:
        pass
    names = [n for _, n in sorted(ranked, reverse=True)]
    # keep one entry per version family, so we don't retry near-identical aliases
    seen, picked = set(), []
    for n in names:
        family = re.sub(r"-(preview|latest|\d{3,}).*$", "", n)
        if family not in seen:
            seen.add(family)
            picked.append(n)
    if not picked:  # listing failed (e.g. network blip): use a fallback now, but try listing again next time
        return ["gemini-2.5-flash"]
    _gemini_list[:] = picked[:4]
    return _gemini_list


def _gemini(system: str, prompt: str, key: str) -> tuple[str, str]:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    }
    errors = []
    deadline = time.monotonic() + 75  # then give up and let Groq answer
    for model in _gemini_models(key):
        left = deadline - time.monotonic()
        if left < 10:
            errors.append("time budget used up")
            break
        try:
            r = httpx.post(f"{GEMINI_BASE}/models/{model}:generateContent", json=body,
                           headers={"x-goog-api-key": key}, timeout=min(60, left))
        except httpx.TimeoutException:
            errors.append(f"{model}: timed out")
            continue
        if r.status_code in (429, 500, 503, 404):  # busy, quota or gone: try the next model
            errors.append(f"{model}: HTTP {r.status_code}")
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini {model}: HTTP {r.status_code} {r.text[:300]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought")), f"gemini:{model}"
    raise RuntimeError("Gemini unavailable (" + ", ".join(errors) + ")")


# ---------- OpenAI-compatible providers: Groq, DeepSeek, OpenRouter ----------
DEEPSEEK_BASE = "https://api.deepseek.com"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_picked: dict[str, str] = {}   # provider -> auto-picked model


def _groq_pick(key: str) -> str:
    r = httpx.get(f"{GROQ_BASE}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    for pref in ("llama-3.3-70b", "gpt-oss-120b", "70b", "llama"):
        for i in ids:
            if pref in i and "guard" not in i and "whisper" not in i:
                return i
    if not ids:
        raise RuntimeError("Groq returned no models")
    return ids[0]


def _openrouter_pick(key: str) -> str:
    """Prefer a free DeepSeek model, then any other free model with a large context."""
    r = httpx.get(f"{OPENROUTER_BASE}/models", timeout=30)
    r.raise_for_status()
    free = [m for m in r.json().get("data", []) if m.get("id", "").endswith(":free")]
    if not free:
        raise RuntimeError("OpenRouter lists no free models right now")

    def rank(m):
        mid = m["id"].lower()
        return ("deepseek" in mid, "r1" not in mid and "think" not in mid,  # prefer fast, non-reasoning chat models
                m.get("context_length") or 0)

    return max(free, key=rank)["id"]


PROVIDERS = {
    # name: (key setting, base URL, model setting, default model or picker)
    "groq": ("GROQ_API_KEY", GROQ_BASE, "GROQ_MODEL", "llama-3.3-70b-versatile", _groq_pick),
    "deepseek": ("DEEPSEEK_API_KEY", DEEPSEEK_BASE, "DEEPSEEK_MODEL", "deepseek-chat", None),
    "openrouter": ("OPENROUTER_API_KEY", OPENROUTER_BASE, "OPENROUTER_MODEL", None, _openrouter_pick),
}


def _openai_compat(name: str, system: str, prompt: str, key: str) -> tuple[str, str]:
    _, base, model_setting, default, picker = PROVIDERS[name]
    forced = config.get(model_setting)
    model = forced or _picked.get(name) or default or picker(key)
    _picked.setdefault(name, model)
    headers = {"Authorization": f"Bearer {key}"}
    if name == "openrouter":
        headers.update({"HTTP-Referer": config.get("PUBLIC_URL") or "http://localhost", "X-Title": "Faceless Shorts"})

    def call(m: str, json_mode: bool = True) -> httpx.Response:
        body = {"model": m, "temperature": 0.9,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return httpx.post(f"{base}/chat/completions", timeout=120, headers=headers, json=body)

    r = call(model)
    if r.status_code in (400, 404) and "model" in r.text.lower() and not forced and picker:
        model = picker(key)          # model retired or renamed: pick a current one
        _picked[name] = model
        r = call(model)
    if r.status_code == 400 and "response_format" in r.text:
        r = call(model, json_mode=False)  # some models don't support JSON mode; the prompt still asks for JSON
    if r.status_code >= 400:
        raise RuntimeError(f"{name} {model}: HTTP {r.status_code} {r.text[:300]}")
    content = r.json()["choices"][0]["message"].get("content") or ""
    if not content.strip():
        raise RuntimeError(f"{name} {model}: empty reply")
    return content, f"{name}:{model}"


ALL_PROVIDERS = ["gemini", "groq", "openrouter", "deepseek"]
KEY_FOR = {"gemini": "GEMINI_API_KEY", **{n: p[0] for n, p in PROVIDERS.items()}}


def provider_order() -> list[str]:
    """Admin-configurable order (LLM_ORDER), e.g. 'gemini,deepseek,groq'. Unknown names are ignored."""
    raw = [p.strip().lower() for p in (config.get("LLM_ORDER") or "").split(",") if p.strip()]
    order = [p for p in raw if p in ALL_PROVIDERS]
    return order + [p for p in ALL_PROVIDERS if p not in order]


def _complete_json(system: str, prompt: str) -> tuple[dict, str]:
    errors = []
    for name in provider_order():
        key = config.get(KEY_FOR[name])
        if not key:
            continue
        try:
            if name == "gemini":
                text, used = _gemini(system, prompt, key)
            else:
                text, used = _openai_compat(name, system, prompt, key)
            return _parse_json(text), used
        except Exception as e:  # try the next provider
            errors.append(str(e)[:300])
    if not errors:
        raise RuntimeError("No AI key set. Add a Gemini, Groq, OpenRouter or DeepSeek key in Settings.")
    raise RuntimeError("All AI providers failed: " + " | ".join(errors))


def generate_script(topic: str, duration: int = 45, style: str = "engaging, curious",
                    language: str = "English", niche: str = "", notes: str = "") -> dict:
    data, used = _complete_json(SCRIPT_SYSTEM, build_prompt(topic, duration, style, language, niche, notes))
    script = _normalize(data, topic)
    script["model"] = used
    return script


def generate_topics(niche: str, notes: str = "", language: str = "English",
                    avoid: list[str] | None = None, n: int = 10) -> list[str]:
    avoid = (avoid or [])[-80:]
    prompt = (f"Channel niche: {niche}\n"
              + (f"Channel guidelines: {notes}\n" if notes else "")
              + f"Write the topics in {language}.\n"
              + (("Already used (do not repeat):\n- " + "\n- ".join(avoid) + "\n") if avoid else "")
              + f"Give {n} new topics.")
    data, _ = _complete_json(TOPICS_SYSTEM, prompt)
    seen = {a.lower() for a in avoid}
    topics = []
    for t in data.get("topics") or []:
        t = str(t).strip().strip('"').strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            topics.append(t[:200])
    if not topics:
        raise RuntimeError("The AI returned no topics")
    return topics[:n]
