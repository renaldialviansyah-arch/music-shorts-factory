#!/usr/bin/env python3
"""Music Shorts Factory - main program: script -> narration -> music -> animation -> MP4."""
import argparse
import json
import re
import shutil
import sys
import traceback
from pathlib import Path

from parse_script import ScriptError, parse_episode
import make_audio
import render_video

ROOT = Path(__file__).resolve().parent.parent
FPS = 30
LEAD_IN = 0.35
TAIL = 0.6
LAST_TAIL = 1.0
MIN_SCENE = 3.5
MIN_TOTAL = 30.0
MAX_TOTAL = 60.0
GUESS_WORDS_PER_SECOND = 2.5


def str2bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def resolve_episode(arg):
    path = Path(arg)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    episodes_dir = (ROOT / "episodes").resolve()
    if episodes_dir not in path.parents:
        raise ScriptError("The episode file must be inside the 'episodes' folder.")
    if not path.exists():
        names = sorted(p.name for p in episodes_dir.glob("*.txt"))
        raise ScriptError(
            f"Episode file not found: {arg}. Files in the episodes folder: {', '.join(names) or 'none'}. "
            "Check spelling in the 'Script file' box when you press Run workflow."
        )
    return path


def word_timings(text, start, duration):
    words = text.split()
    weights = [len(w) + 1 + (4 if re.search(r"[.,!?;:]$", w) else 0) for w in words]
    total_weight = float(sum(weights)) or 1.0
    cursor = start
    result = []
    for word, weight in zip(words, weights):
        span = duration * weight / total_weight
        result.append({"w": word, "s": round(cursor, 3), "e": round(cursor + span, 3)})
        cursor += span
    return result


def run(args):
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} is not installed on this machine.")

    episode_path = resolve_episode(args.episode)
    episode = parse_episode(str(episode_path))
    scenes = episode["scenes"]
    print(f"Episode: {episode['title']} ({len(scenes)} scenes)", flush=True)

    work = ROOT / "work"
    out_dir = ROOT / args.out
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    want_voice = str2bool(args.voice)
    want_music = str2bool(args.music)

    wavs = []
    if want_voice:
        print("Step 1/5: creating narration...", flush=True)
        wavs, engine = make_audio.synthesize_scenes([s["narration"] for s in scenes], work)
        if wavs:
            print(f"  Narration engine: {engine}")
        else:
            print("WARNING: no voice engine worked. Continuing with captions only (no spoken audio).")
    else:
        print("Step 1/5: narration skipped (voice turned off).")

    durations = []
    narration_lengths = []
    for index, scene in enumerate(scenes):
        words = scene["narration"].split()
        narration_len = make_audio.probe_duration(wavs[index]) if wavs else len(words) / GUESS_WORDS_PER_SECOND
        tail = LAST_TAIL if index == len(scenes) - 1 else TAIL
        duration = max(MIN_SCENE, LEAD_IN + narration_len + tail)
        if scene["seconds"]:
            duration = max(duration, scene["seconds"])
        narration_lengths.append(narration_len)
        durations.append(duration)
    total = sum(durations)

    print("Scene timing:")
    for index, duration in enumerate(durations):
        print(f"  scene {index + 1}: {duration:.1f}s")
    print(f"  TOTAL: {total:.1f}s (target {MIN_TOTAL:.0f}-{MAX_TOTAL:.0f}s)", flush=True)
    if total < MIN_TOTAL:
        raise ScriptError(
            f"The video would be only {total:.1f} seconds. Add more narration (about 2.5 words per second) "
            f"or more scenes to reach {MIN_TOTAL:.0f} seconds."
        )
    if total > MAX_TOTAL:
        raise ScriptError(
            f"The video would be {total:.1f} seconds, over the {MAX_TOTAL:.0f}-second limit. "
            "Shorten the narration or remove a scene."
        )

    cursor = 0.0
    timeline_scenes = []
    for index, scene in enumerate(scenes):
        timeline_scenes.append(
            {
                "start": round(cursor, 3),
                "dur": round(durations[index], 3),
                "visual": scene["visual"],
                "kicker": scene["kicker"],
                "headline": scene["headline"],
                "year": scene["year"],
                "words": word_timings(scene["narration"], LEAD_IN, narration_lengths[index]),
            }
        )
        cursor += durations[index]
    timeline = {
        "fps": FPS,
        "width": 1080,
        "height": 1920,
        "total": round(total, 3),
        "theme": episode["theme"],
        "channel": episode["channel"],
        "scenes": timeline_scenes,
    }
    (work / "timeline.json").write_text(json.dumps(timeline, indent=2), encoding="utf-8")

    print("Step 2/5: building the audio track...", flush=True)
    narration_track = make_audio.build_narration_track(wavs, durations, LEAD_IN, work) if wavs else None
    music_track = None
    if want_music:
        music_track = work / "music.wav"
        make_audio.make_music(music_track, total, episode["mood"])
    final_audio = work / "final_audio.wav"
    make_audio.mix_audio(narration_track, music_track, total, final_audio)

    print("Step 3/5: rendering the animation (this is the slow part)...", flush=True)
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", episode_path.stem).strip("-") or "short"
    video_path = out_dir / f"{stem}.mp4"
    render_video.render(
        timeline, final_audio, video_path,
        ROOT / "templates" / "scene.html", out_dir / "render-log.txt",
    )

    print("Step 4/5: writing upload notes...", flush=True)
    notes = [
        f"TITLE: {episode['title']}",
        "",
        "DESCRIPTION:",
        " ".join(s["narration"] for s in scenes),
        "",
        episode["hashtags"],
        "",
        "REMINDER: fact-check every claim before uploading. AI-written scripts can contain mistakes.",
        "Narration, visuals and music in this video were generated automatically. "
        "No commercial music, artwork or lyrics were used.",
    ]
    (out_dir / f"{stem}-upload-notes.txt").write_text("\n".join(notes), encoding="utf-8")

    print(f"Step 5/5: done. Video saved as {video_path.name}", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", required=True)
    parser.add_argument("--voice", default="true")
    parser.add_argument("--music", default="true")
    parser.add_argument("--out", default="output")
    args = parser.parse_args()
    try:
        return run(args)
    except ScriptError as error:
        print(f"\nERROR in your episode file: {error}")
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"\nERROR: {error}")
        print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
