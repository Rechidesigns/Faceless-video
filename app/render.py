"""ffmpeg helpers: scene clips, concatenation, final mix with captions and music."""
import os
import subprocess
from pathlib import Path

from . import config

W, H, FPS = 1080, 1920, 30
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]


def run(args: list[str], cwd: Path | None = None) -> None:
    if not config.FFMPEG:
        raise RuntimeError("ffmpeg not found. Install it with: winget install Gyan.FFmpeg")
    p = subprocess.run([config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args],
                       cwd=cwd, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr.strip()[-800:]}")


def duration(path: Path) -> float:
    p = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        raise RuntimeError(f"could not read duration of {path.name}")


def to_wav(src: Path, out: Path, pad: float) -> Path:
    run(["-i", str(src), "-af", f"apad=pad_dur={pad}", "-ar", "44100", "-ac", "2",
         "-c:a", "pcm_s16le", str(out)])
    return out


def frames_for(dur: float) -> int:
    return max(1, round(dur * FPS))


def scene_clip(src: Path | None, kind: str, dur: float, out: Path, index: int, start: float = 0.0) -> Path:
    """Render exactly frames_for(dur) frames, so scene lengths never drift from the voiceover."""
    frames = frames_for(dur)
    # clone the last frame if the source runs out early, then stop at the exact frame count
    hold = f"tpad=stop_mode=clone:stop_duration={dur + 1:.3f}"
    cover = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    if kind == "video":
        # slow push-in (up to +7%) keeps every shot moving, even static stock footage
        push = (f"scale=w='trunc({W}*(1+0.07*t/{max(dur, 0.1):.3f})/2)*2':h=-2:eval=frame,"
                f"crop={W}:{H}")
        seek = ["-ss", f"{start:.3f}"] if start > 0 else []
        run([*seek, "-stream_loop", "-1", "-i", str(src), "-an",
             "-vf", f"{cover},{push},setsar=1,fps={FPS},setpts=N/{FPS}/TB,{hold}",
             "-frames:v", str(frames), "-r", str(FPS), *ENC, str(out)])
    elif kind == "image":
        step = 0.18 / frames
        # alternate slow zoom-in / zoom-out for a Ken Burns feel
        z = (f"min(zoom+{step:.6f},1.18)" if index % 2 == 0
             else f"if(eq(on,0),1.18,max(zoom-{step:.6f},1.0))")
        vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
              f"setsar=1,{hold}")
        run(["-i", str(src), "-vf", vf, "-frames:v", str(frames), "-r", str(FPS), *ENC, str(out)])
    else:
        run(["-f", "lavfi", "-i", f"color=c=0x111827:s={W}x{H}:r={FPS}", "-frames:v", str(frames),
             *ENC, str(out)])
    return out


def scene_from_shots(shots: list[tuple[Path | None, str]], dur: float, out: Path, index: int,
                     workdir: Path) -> Path:
    """Fast-cut scene: split the scene's duration across its shots, each starting at a random point."""
    import random
    if len(shots) == 1:
        src, kind = shots[0]
        start = 0.0
        if kind == "video":
            try:
                start = max(0.0, random.uniform(0, max(0.0, duration(src) - dur - 0.2)))
            except RuntimeError:
                pass
        return scene_clip(src, kind, dur, out, index, start)
    # split in whole frames so the shots add up to exactly the scene's frame count
    total = frames_for(dur)
    n = len(shots)
    cuts = [round(total * k / n) for k in range(n + 1)]
    parts = []
    for k, (src, kind) in enumerate(shots):
        d = (cuts[k + 1] - cuts[k]) / FPS
        start = 0.0
        if kind == "video":
            try:
                start = max(0.0, random.uniform(0, max(0.0, duration(src) - d - 0.2)))
            except RuntimeError:
                pass
        parts.append(scene_clip(src, kind, d, workdir / f"{out.stem}_{k}.mp4", index + k, start))
    return concat(parts, out, workdir)


def concat(files: list[Path], out: Path, workdir: Path) -> Path:
    listing = workdir / f"{out.stem}_list.txt"
    listing.write_text("".join(f"file '{f.name}'\n" for f in files), encoding="utf-8")
    args = ["-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy", out.name]
    run(args, cwd=workdir)
    return out


def final_mix(workdir: Path, video: Path, narration: Path, ass: Path | None,
              music: Path | None, music_volume: float, out: Path) -> Path:
    # The voiceover sets the length: if the picture is short it holds its last frame, it never cuts the voice.
    length = duration(narration)
    args = ["-i", video.name, "-i", narration.name]
    if music:
        args += ["-stream_loop", "-1", "-i", str(music)]
    hold = "tpad=stop_mode=clone:stop_duration=5"
    vchain = f"[0:v]{hold}[v]"
    if ass:
        sub = f"subtitles={ass.name}"
        try:  # relative path avoids Windows drive-letter escaping inside the filter string
            fonts = os.path.relpath(config.FONTS, workdir).replace("\\", "/")
            sub += f":fontsdir={fonts}"
        except ValueError:  # different drive on Windows: rely on system fonts
            pass
        vchain = f"[0:v]{hold},{sub}[v]"
    # Loudness-normalize to -14 LUFS (the TikTok / YouTube / Instagram standard) so the voice isn't quiet.
    # loudnorm resamples internally, so convert back to 44.1 kHz afterwards.
    norm = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100"
    if music:
        achain = (f"[2:a]volume={music_volume:.2f}[m];"
                  f"[1:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{norm}[a]")
    else:
        achain = f"[1:a]{norm}[a]"
    args += ["-filter_complex", f"{vchain};{achain}", "-map", "[v]", "-map", "[a]",
             *ENC, "-c:a", "aac", "-b:a", "192k", "-t", f"{length:.3f}", "-movflags", "+faststart", out.name]
    run(args, cwd=workdir)
    return out
