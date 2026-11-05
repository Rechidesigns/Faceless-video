"""Voiceover with edge-tts (free Microsoft neural voices). Returns per-word timings."""
import asyncio
import time
from pathlib import Path

import edge_tts

_voices: list[dict] = []


async def list_voices() -> list[dict]:
    if not _voices:
        for v in await asyncio.wait_for(edge_tts.list_voices(), timeout=20):
            _voices.append({
                "name": v["ShortName"],
                "locale": v["Locale"],
                "gender": v.get("Gender", ""),
            })
        _voices.sort(key=lambda v: (v["locale"], v["name"]))
    return _voices


async def _synth(text: str, voice: str, rate: str, out: Path) -> list[dict]:
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    words = []
    with open(out, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append({"start": start,
                              "end": start + chunk["duration"] / 1e7,
                              "text": chunk["text"]})
    return words


def synth(text: str, voice: str, rate: str, out: Path, waits=(2, 5, 10, 20), timeout: float = 60) -> list[dict]:
    """The free edge-tts service drops out now and then; back off and retry. Never leaves an empty file."""
    last = None
    for attempt in range(len(waits) + 1):
        try:
            # hard limit: the service sometimes stalls without ever timing out
            words = asyncio.run(asyncio.wait_for(_synth(text, voice, rate, out), timeout=timeout))
            if out.exists() and out.stat().st_size > 1000:
                return words
            last = RuntimeError("no audio received")
        except Exception as e:
            last = e
        out.unlink(missing_ok=True)
        if attempt < len(waits):
            time.sleep(waits[attempt])
    raise RuntimeError(f"Voiceover failed after {len(waits) + 1} tries (Microsoft voice service unreachable or busy): {last}")
