"""Runs one video job: script -> voice -> visuals -> captions -> final MP4."""
import json
import random

from . import captions, config, llm, render, topics, tts, visuals

GAP = 0.25        # pause after each scene's narration
END_GAP = 0.8     # longer pause at the end
MUSIC_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}


def music_files() -> list[str]:
    return sorted(p.name for p in config.MUSIC.iterdir() if p.suffix.lower() in MUSIC_EXT)


def run(job: dict, update, log) -> None:
    workdir = config.OUTPUT / job["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    s = job["settings"]

    if not job.get("script"):
        t = job["topic"]
        if not t.get("topic"):
            update(2, "Picking a topic")
            t["topic"] = topics.take_next(job["channel_id"])
            job["title"] = t["topic"]
            log(f"topic: {t['topic']}")
        update(3, "Writing script")
        job["script"] = llm.generate_script(t["topic"], int(t.get("duration", 45)),
                                            t.get("style", "engaging"), t.get("language", "English"),
                                            t.get("niche", ""), t.get("notes", ""))
        log(f"script written by {job['script'].get('model')}")
    script = job["script"]
    job["title"] = script["title"]
    (workdir / "script.json").write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    scenes = script["scenes"]
    n = len(scenes)

    # 1. voiceover, one file per scene so each scene's length is exact
    wavs, durs, words = [], [], []
    offset = 0.0
    for i, sc in enumerate(scenes):
        update(8 + 30 * i / n, f"Voiceover {i + 1}/{n}")
        mp3 = workdir / f"a_{i:02d}.mp3"
        scene_words = tts.synth(sc["narration"], s["voice"], s.get("rate", "+0%"), mp3)
        wav = render.to_wav(mp3, workdir / f"a_{i:02d}.wav", END_GAP if i == n - 1 else GAP)
        d = render.duration(wav)
        if scene_words:
            words += [{**w, "start": w["start"] + offset, "end": w["end"] + offset} for w in scene_words]
        elif s.get("caption_style", "bold") != "none":
            fallback = captions.whisper_words(wav, offset) or captions.estimate_words(sc["narration"], offset, d - GAP)
            words += fallback
            log(f"scene {i + 1}: no word timings from TTS, used fallback")
        wavs.append(wav)
        durs.append(d)
        offset += d

    # 2. visuals
    used: set = set()
    clips, blank = [], 0
    have_sources = any(config.get(k) for k in ("PIXABAY_API_KEY", "PEXELS_API_KEY", "CLOUDFLARE_API_TOKEN"))
    for i, sc in enumerate(scenes):
        update(40 + 40 * i / n, f"Visuals {i + 1}/{n}")
        src, kind = visuals.fetch(i, sc, s.get("visual_mode", "stock"), workdir, used, log)
        log(f"scene {i + 1}: {kind}")
        blank += kind == "none"
        if have_sources and blank > n / 3:
            raise RuntimeError(f"{blank} of {n} scenes got no picture (network or API problem), so the video "
                               f"was stopped instead of coming out mostly blank. Retry later.")
        clips.append(render.scene_clip(src, kind, durs[i], workdir / f"s_{i:02d}.mp4", i))

    # 3. assemble
    update(82, "Assembling")
    video = render.concat(clips, workdir / "video.mp4", workdir)
    narration = render.concat(wavs, workdir / "narration.wav", workdir)
    ass = captions.write_ass(words, workdir / "captions.ass", s.get("caption_style", "bold"))

    music = None
    choice = s.get("music", "none")
    tracks = music_files()
    if choice == "random" and tracks:
        music = config.MUSIC / random.choice(tracks)
    elif choice not in ("none", "random") and choice in tracks:
        music = config.MUSIC / choice
    if music:
        log(f"music: {music.name}")

    update(88, "Rendering final video")
    render.final_mix(workdir, video, narration, ass, music, float(s.get("music_volume", 0.12)),
                     workdir / "final.mp4")

    # 4. clean up intermediates
    for p in workdir.iterdir():
        if p.name not in ("final.mp4", "script.json", "job.json", "captions.ass"):
            p.unlink(missing_ok=True)
    job["length"] = round(sum(durs), 1)
