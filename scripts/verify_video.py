#!/usr/bin/env python3
"""Checks that the MP4 is a valid 1080x1920 H.264/AAC Short of 30-60 seconds."""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)


def check(path, min_seconds, max_seconds):
    problems = []
    warnings = []
    path = Path(path)
    print(f"\nChecking {path.name}")
    if not path.exists():
        print("  FAIL: file does not exist")
        return 1
    size_mb = path.stat().st_size / 1_000_000
    print(f"  size: {size_mb:.1f} MB")
    if path.stat().st_size < 300_000:
        problems.append("file is suspiciously small")

    try:
        info = probe(path)
    except Exception as error:  # noqa: BLE001
        print(f"  FAIL: cannot read file ({error})")
        return 1

    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if not video:
        problems.append("no video stream")
    else:
        print(f"  video: {video.get('codec_name')} {video.get('width')}x{video.get('height')} {video.get('pix_fmt')}")
        if video.get("codec_name") != "h264":
            problems.append("video is not H.264")
        if (video.get("width"), video.get("height")) != (1080, 1920):
            problems.append("video is not 1080x1920")
        if video.get("pix_fmt") != "yuv420p":
            problems.append("pixel format is not yuv420p")
        try:
            num, den = video.get("r_frame_rate", "0/1").split("/")
            fps = float(num) / float(den)
            print(f"  frame rate: {fps:.2f}")
            if not 29.0 <= fps <= 31.0:
                problems.append("frame rate is not about 30")
        except Exception:  # noqa: BLE001
            warnings.append("could not read frame rate")
    if not audio:
        problems.append("no audio stream")
    else:
        print(f"  audio: {audio.get('codec_name')}")
        if audio.get("codec_name") != "aac":
            problems.append("audio is not AAC")

    duration = float(info["format"].get("duration", 0))
    print(f"  duration: {duration:.1f} s")
    if duration < min_seconds - 0.25 or duration > max_seconds + 0.25:
        problems.append(f"duration {duration:.1f}s is outside {min_seconds:.0f}-{max_seconds:.0f}s")

    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, text=True)
    if decode.returncode != 0 or decode.stderr.strip():
        problems.append("decode test found errors: " + decode.stderr.strip()[:200])
    else:
        print("  decode test: OK")

    volume = subprocess.run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                            capture_output=True, text=True)
    match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", volume.stderr)
    if match:
        mean = float(match.group(1))
        print(f"  mean volume: {mean:.1f} dB")
        if mean < -55:
            warnings.append("audio is nearly silent (fine only if you turned voice and music off)")

    for warning in warnings:
        print(f"  WARNING: {warning}")
    if problems:
        for problem in problems:
            print(f"  FAIL: {problem}")
        return 1
    print("  PASS: this MP4 is ready for you to review.")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*")
    parser.add_argument("--dir")
    parser.add_argument("--min", type=float, default=30.0)
    parser.add_argument("--max", type=float, default=60.0)
    args = parser.parse_args()
    files = list(args.files)
    if args.dir:
        files += sorted(str(p) for p in Path(args.dir).glob("*.mp4"))
    if not files:
        print("ERROR: no MP4 file was found to verify.")
        return 1
    failures = sum(check(f, args.min, args.max) for f in files)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
