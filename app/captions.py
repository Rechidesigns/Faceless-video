"""Word-timed captions as an ASS subtitle file, burned in by ffmpeg."""
from pathlib import Path

# Fonts are bundled in /fonts (Anton, Poppins; SIL Open Font License) so captions look the same everywhere.
STYLES = {
    "bold": {"font": "Anton", "size": 118, "outline": 7, "shadow": 3,
             "highlight": True, "upper": True, "max_words": 3, "margin_v": 640},
    "minimal": {"font": "Poppins SemiBold", "size": 64, "outline": 4, "shadow": 2,
                "highlight": False, "upper": False, "max_words": 5, "margin_v": 420},
}
HIGHLIGHT = r"{\c&H0000D7FF&\fscx112\fscy112}"  # gold (ASS colours are BGR)


def _ts(t: float) -> str:
    cs = int(round(max(t, 0) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").strip()


def estimate_words(text: str, offset: float, duration: float) -> list[dict]:
    """Spread words over the clip, weighted by length (used only if timings are missing)."""
    tokens = text.split()
    if not tokens or duration <= 0:
        return []
    total = sum(len(t) + 2 for t in tokens)
    t, out = offset, []
    for tok in tokens:
        d = duration * (len(tok) + 2) / total
        out.append({"start": t, "end": t + d * 0.9, "text": tok})
        t += d
    return out


def whisper_words(wav: Path, offset: float) -> list[dict]:
    """Optional fallback: faster-whisper word timestamps (CPU)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return []
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(wav), word_timestamps=True)
    return [{"start": w.start + offset, "end": w.end + offset, "text": w.word.strip()}
            for seg in segments for w in (seg.words or [])]


def _chunks(words: list[dict], max_words: int) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        gap = w["start"] - cur[-1]["end"] if cur else 0
        if cur and (len(cur) >= max_words or gap > 0.35 or
                    sum(len(x["text"]) for x in cur) + len(w["text"]) > 18):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def write_ass(words: list[dict], out: Path, style: str = "bold") -> Path | None:
    if style not in STYLES or not words:
        return None
    s = STYLES[style]
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{s['font']},{s['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,"
        f"-1,0,0,0,100,100,0,0,1,{s['outline']},{s['shadow']},2,70,70,{s['margin_v']},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    chunks = _chunks(words, s["max_words"])
    for ci, chunk in enumerate(chunks):
        nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else chunk[-1]["end"] + 0.6
        chunk_end = min(chunk[-1]["end"] + 0.4, nxt)
        texts = [_clean(w["text"]) for w in chunk]
        if s["upper"]:
            texts = [t.upper() for t in texts]
        if not s["highlight"]:
            lines.append(f"Dialogue: 0,{_ts(chunk[0]['start'])},{_ts(chunk_end)},Default,,0,0,0,,{' '.join(texts)}")
            continue
        for wi, w in enumerate(chunk):
            end = chunk[wi + 1]["start"] if wi + 1 < len(chunk) else chunk_end
            parts = [f"{HIGHLIGHT}{t}{{\\r}}" if j == wi else t for j, t in enumerate(texts)]
            lines.append(f"Dialogue: 0,{_ts(w['start'])},{_ts(end)},Default,,0,0,0,,{' '.join(parts)}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
