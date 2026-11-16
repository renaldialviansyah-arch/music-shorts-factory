#!/usr/bin/env python3
"""Free narration (Piper, with espeak-ng fallback) and original generated music."""
import array
import math
import random
import shutil
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

PIPER_NAME = "en_US-lessac-medium"
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/"


def run_quiet(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, **kwargs)


def probe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"Could not read the length of {path}: {result.stderr.strip()}")


def _download(url, destination, tries=3):
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=90) as response, open(destination, "wb") as handle:
                shutil.copyfileobj(response, handle)
            return True
        except Exception as error:  # noqa: BLE001
            print(f"  download attempt {attempt} failed: {error}", flush=True)
            time.sleep(3)
    return False


def ensure_piper_model(models_dir):
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx = models_dir / f"{PIPER_NAME}.onnx"
    meta = models_dir / f"{PIPER_NAME}.onnx.json"
    if not (onnx.exists() and onnx.stat().st_size > 1_000_000 and meta.exists()):
        print("  Downloading the free Piper voice model (about 60 MB)...", flush=True)
        if not (_download(PIPER_BASE + onnx.name, onnx) and _download(PIPER_BASE + meta.name, meta)):
            return None
        if onnx.stat().st_size < 1_000_000:
            return None
    return str(onnx)


def synth_piper(text, model, out_wav):
    variants = [
        [sys.executable, "-m", "piper", "--model", model, "--output_file", out_wav],
        [sys.executable, "-m", "piper", "-m", model, "-f", out_wav],
    ]
    exe = shutil.which("piper")
    if exe:
        variants.append([exe, "--model", model, "--output_file", out_wav])
    for cmd in variants:
        Path(out_wav).unlink(missing_ok=True)
        try:
            result = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=300)
        except Exception as error:  # noqa: BLE001
            print(f"  piper call failed: {error}")
            continue
        if result.returncode == 0 and Path(out_wav).exists() and Path(out_wav).stat().st_size > 2000:
            return True
        tail = result.stderr.decode("utf-8", "ignore").strip().splitlines()[-2:]
        print(f"  piper variant failed: {' | '.join(tail)}")
    return False


def synth_espeak(text, out_wav):
    if not shutil.which("espeak-ng"):
        return False
    Path(out_wav).unlink(missing_ok=True)
    result = subprocess.run(
        ["espeak-ng", "-v", "en-us", "-s", "155", "-p", "40", "-w", out_wav, "--stdin"],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    return result.returncode == 0 and Path(out_wav).exists() and Path(out_wav).stat().st_size > 2000


def synthesize_scenes(texts, workdir):
    """Returns (list_of_wav_paths, engine_name). Empty list if no voice engine works."""
    workdir.mkdir(parents=True, exist_ok=True)
    engines = []
    model = ensure_piper_model(Path(__file__).resolve().parent.parent / ".cache" / "models")
    if model:
        engines.append(("piper", lambda text, out: synth_piper(text, model, out)))
    else:
        print("  Piper voice model unavailable; will try the fallback voice.")
    engines.append(("espeak-ng", synth_espeak))

    for name, function in engines:
        paths = []
        ok = True
        for index, text in enumerate(texts):
            out = workdir / f"narration_{index:02d}.wav"
            print(f"  [{name}] scene {index + 1}/{len(texts)}", flush=True)
            if not function(text, str(out)):
                ok = False
                break
            paths.append(out)
        if ok:
            return paths, name
        print(f"  Voice engine '{name}' failed. Trying the next option...")
    return [], None


def build_narration_track(wavs, durations, lead_in, workdir):
    """One narration file whose length equals the whole video, aligned scene by scene."""
    parts = []
    for index, duration in enumerate(durations):
        out = workdir / f"scene_audio_{index:02d}.wav"
        if wavs:
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(wavs[index]),
                "-af", f"aresample=44100,aformat=channel_layouts=mono,adelay={int(lead_in * 1000)}:all=1,apad",
                "-t", f"{duration:.3f}", "-ar", "44100", "-ac", "1", str(out),
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", f"{duration:.3f}", str(out),
            ]
        result = run_quiet(cmd)
        if result.returncode != 0:
            raise RuntimeError("FFmpeg could not prepare narration: " + result.stderr.decode("utf-8", "ignore")[-400:])
        parts.append(out)
    list_file = workdir / "narration_list.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    full = workdir / "narration_full.wav"
    result = run_quiet(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(full)]
    )
    if result.returncode != 0:
        raise RuntimeError("FFmpeg could not join narration: " + result.stderr.decode("utf-8", "ignore")[-400:])
    return full


CHORDS = {
    "minor": [[110.00, 130.81, 164.81, 220.00], [87.31, 110.00, 130.81, 174.61],
              [130.81, 164.81, 196.00, 261.63], [98.00, 123.47, 146.83, 196.00]],
    "major": [[130.81, 164.81, 196.00, 261.63], [98.00, 123.47, 146.83, 196.00],
              [110.00, 130.81, 164.81, 220.00], [87.31, 110.00, 130.81, 174.61]],
    "dreamy": [[87.31, 130.81, 164.81, 220.00], [98.00, 146.83, 174.61, 246.94],
               [110.00, 130.81, 196.00, 261.63], [82.41, 123.47, 164.81, 196.00]],
}


def make_music(path, seconds, mood="minor", sr=22050, bpm=72):
    """Original ambient pad + soft pulse, generated from math. No samples, no copyrighted material."""
    chords = CHORDS.get(mood, CHORDS["minor"])
    total = int((seconds + 1.0) * sr)
    chord_len = 8.0
    beat = 60.0 / bpm
    rnd = random.Random(7)
    sin = math.sin
    exp = math.exp
    two_pi = 2.0 * math.pi
    samples = [0.0] * total
    peak = 1e-9
    for i in range(total):
        t = i / sr
        k = int(t // chord_len)
        x = (t - k * chord_len) / chord_len
        c = min(1.0, max(0.0, (x - 0.7) / 0.3))
        w_next = c * c * (3 - 2 * c)
        w_cur = 1.0 - w_next
        pad = 0.0
        for f in chords[k % len(chords)]:
            pad += w_cur * (sin(two_pi * f * t) + 0.3 * sin(two_pi * 2 * f * t))
        if w_next > 0.0:
            for f in chords[(k + 1) % len(chords)]:
                pad += w_next * (sin(two_pi * f * t) + 0.3 * sin(two_pi * 2 * f * t))
        pad = pad / 5.2 * (0.8 + 0.2 * sin(two_pi * 0.15 * t))
        tb = t % beat
        kick = sin(two_pi * 52.0 * tb) * exp(-tb * 9.0) * 0.45
        th = (t + beat / 2.0) % beat
        hat = (rnd.random() * 2 - 1) * exp(-th * 70.0) * 0.06 if th < 0.12 else 0.0
        value = pad * 0.6 + kick + hat
        samples[i] = value
        if abs(value) > peak:
            peak = abs(value)
    scale = 0.85 * 32767 / peak
    data = array.array("h", (int(v * scale) for v in samples))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(data.tobytes())


def mix_audio(narration, music, total, out):
    """Mix narration + music (music ducks under the voice). Always produces a file."""
    out = str(out)
    fade_start = max(0.0, total - 2.0)
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    tail = ["-t", f"{total:.3f}", "-ar", "44100", "-ac", "2", out]

    if narration and music:
        attempts = [
            f"[1:a]aresample=44100,volume=0.30,afade=t=in:d=1.5,afade=t=out:st={fade_start:.2f}:d=2[m];"
            "[0:a]asplit=2[n][sc];"
            "[m][sc]sidechaincompress=threshold=0.04:ratio=8:attack=20:release=500[duck];"
            "[n][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]",
            f"[1:a]aresample=44100,volume=0.15,afade=t=in:d=1.5,afade=t=out:st={fade_start:.2f}:d=2[m];"
            "[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
        ]
        for graph in attempts:
            result = run_quiet(base + ["-i", str(narration), "-i", str(music),
                                       "-filter_complex", graph, "-map", "[a]"] + tail)
            if result.returncode == 0 and Path(out).exists():
                return
            print("  audio mix attempt failed, trying a simpler mix...")
        raise RuntimeError("FFmpeg could not mix the audio.")
    if narration:
        cmd = base + ["-i", str(narration)] + tail
    elif music:
        cmd = base + ["-i", str(music), "-af",
                      f"aresample=44100,volume=0.6,afade=t=in:d=1.5,afade=t=out:st={fade_start:.2f}:d=2"] + tail
    else:
        cmd = base + ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"] + tail
    result = run_quiet(cmd)
    if result.returncode != 0:
        raise RuntimeError("FFmpeg audio step failed: " + result.stderr.decode("utf-8", "ignore")[-400:])
