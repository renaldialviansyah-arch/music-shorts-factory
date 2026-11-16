
#!/usr/bin/env python3
"""Music Shorts Factory - script -> narration -> music -> animation -> MP4."""

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

try:
    from load_approved_media import load_approved_media
except ImportError:
    def load_approved_media(manifest_path, episode_stem, work_dir, scene_count):
        print(
            "WARNING: archive media loader not found. "
            "Using original SVG visuals."
        )
        return {}


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
    return str(value).strip().lower() in (
        "1", "true", "yes", "y", "on"
    )


def resolve_episode(arg):
    path = Path(arg)

    if not path.is_absolute():
        path = ROOT / path

    path = path.resolve()
    episodes_dir = (ROOT / "episodes").resolve()

    if episodes_dir not in path.parents:
        raise ScriptError(
            "The episode file must be inside the 'episodes' folder."
        )

    if not path.exists():
        names = sorted(
            p.name for p in episodes_dir.glob("*.txt")
        )
        raise ScriptError(
            f"Episode file not found: {arg}. "
            f"Files in the episodes folder: "
            f"{', '.join(names) or 'none'}. "
            "Check spelling in the 'Script file' box "
            "when you press Run workflow."
        )

    return path


def word_timings(text, start, duration):
    words = text.split()

    weights = [
        len(w) + 1 + (
            4 if re.search(r"[.,!?;:]$", w) else 0
        )
        for w in words
    ]

    total_weight = float(sum(weights)) or 1.0
    cursor = start
    result = []

    for word, weight in zip(words, weights):
        span = duration * weight / total_weight

        result.append({
            "w": word,
            "s": round(cursor, 3),
            "e": round(cursor + span, 3),
        })

        cursor += span

    return result


def run(args):

    # Check required system tools.
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(
                f"{tool} is not installed on this machine."
            )

    # Read and validate episode.
    episode_path = resolve_episode(args.episode)
    episode = parse_episode(str(episode_path))
    scenes = episode["scenes"]

    print(
        f"Episode: {episode['title']} ({len(scenes)} scenes)",
        flush=True
    )

    work = ROOT / "work"
    out_dir = ROOT / args.out

    if work.exists():
        shutil.rmtree(work)

    work.mkdir(parents=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    want_voice = str2bool(args.voice)
    want_music = str2bool(args.music)

    # STEP 1: Generate narration.
    # Existing Kokoro audio implementation is preserved.
    wavs = []

    if want_voice:
        print(
            "Step 1/5: creating narration...",
            flush=True
        )

        wavs, engine = make_audio.synthesize_scenes(
            [s["narration"] for s in scenes],
            work
        )

        if wavs:
            print(f"  Narration engine: {engine}")
        else:
            print(
                "WARNING: no voice engine worked. "
                "Continuing with captions only (no spoken audio)."
            )
    else:
        print(
            "Step 1/5: narration skipped (voice turned off)."
        )

    # Calculate scene durations.
    durations = []
    narration_lengths = []

    for index, scene in enumerate(scenes):
        words = scene["narration"].split()

        narration_len = (
            make_audio.probe_duration(wavs[index])
            if wavs
            else len(words) / GUESS_WORDS_PER_SECOND
        )

        tail = (
            LAST_TAIL
            if index == len(scenes) - 1
            else TAIL
        )

        duration = max(
            MIN_SCENE,
            LEAD_IN + narration_len + tail
        )

        if scene["seconds"]:
            duration = max(duration, scene["seconds"])

        narration_lengths.append(narration_len)
        durations.append(duration)

    total = sum(durations)

    print("Scene timing:")

    for index, duration in enumerate(durations):
        print(
            f"  scene {index + 1}: {duration:.1f}s"
        )

    print(
        f"  TOTAL: {total:.1f}s "
        f"(target {MIN_TOTAL:.0f}-{MAX_TOTAL:.0f}s)",
        flush=True
    )

    if total < MIN_TOTAL:
        raise ScriptError(
            f"The video would be only {total:.1f} seconds. "
            f"Add more narration (about 2.5 words per second) "
            f"or more scenes to reach {MIN_TOTAL:.0f} seconds."
        )

    if total > MAX_TOTAL:
        raise ScriptError(
            f"The video would be {total:.1f} seconds, "
            f"over the {MAX_TOTAL:.0f}-second limit. "
            "Shorten the narration or remove a scene."
        )

    # STEP 2: Load manually approved media.
    # Pending assets are never passed to the renderer.
    manifest_path = Path(args.media_manifest)

    if not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path

    media_by_scene = load_approved_media(
        manifest_path,
        episode_path.stem,
        work,
        len(scenes)
    )

    # Build visual timeline.
    cursor = 0.0
    timeline_scenes = []

    for index, scene in enumerate(scenes):
        media_item = media_by_scene.get(index + 1, {})

        # Backward compatibility with the old loader format.
        if isinstance(media_item, str):
            media_url = media_item
            media_label = "HISTORICAL ARCHIVE" if media_url else ""
        else:
            media_url = media_item.get("url", "")
            media_label = media_item.get("label", "")

        timeline_scenes.append({
            "start": round(cursor, 3),
            "dur": round(durations[index], 3),
            "visual": scene["visual"],
            "kicker": scene["kicker"],
            "headline": scene["headline"],
            "year": scene["year"],

            # Approved archive or AI reconstruction image.
            # Empty URL keeps the original SVG visual active.
            "media_url": media_url,
            "media_label": media_label,

            "words": word_timings(
                scene["narration"],
                LEAD_IN,
                narration_lengths[index]
            ),
        })

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

    (work / "timeline.json").write_text(
        json.dumps(timeline, indent=2),
        encoding="utf-8"
    )

    # STEP 3: Build audio track.
    # Existing Kokoro and original music pipeline are preserved.
    print(
        "Step 2/5: building the audio track...",
        flush=True
    )

    narration_track = (
        make_audio.build_narration_track(
            wavs,
            durations,
            LEAD_IN,
            work
        )
        if wavs
        else None
    )

    music_track = None

    if want_music:
        music_track = work / "music.wav"

        make_audio.make_music(
            music_track,
            total,
            episode["mood"]
        )

    final_audio = work / "final_audio.wav"

    make_audio.mix_audio(
        narration_track,
        music_track,
        total,
        final_audio
    )

    # STEP 4: Render video.
    print(
        "Step 3/5: rendering the animation (this is the slow part)...",
        flush=True
    )

    stem = (
        re.sub(
            r"[^a-zA-Z0-9_-]+",
            "-",
            episode_path.stem
        ).strip("-")
        or "short"
    )

    video_path = out_dir / f"{stem}.mp4"

    render_video.render(
        timeline,
        final_audio,
        video_path,
        ROOT / "templates" / "scene.html",
        out_dir / "render-log.txt",
    )

    # STEP 5: Write upload notes.
    print(
        "Step 4/5: writing upload notes...",
        flush=True
    )

    notes = [
        f"TITLE: {episode['title']}",
        "",
        "DESCRIPTION:",
        " ".join(s["narration"] for s in scenes),
        "",
        episode["hashtags"],
        "",
        "REMINDER: fact-check every claim before uploading. "
        "AI-written scripts can contain mistakes.",
        "AI reconstructions, when used, must be visibly labelled "
        "AI RECONSTRUCTION.",
        "Approved historical archive media must retain its "
        "source and license attribution.",
    ]

    (
        out_dir / f"{stem}-upload-notes.txt"
    ).write_text(
        "\n".join(notes),
        encoding="utf-8"
    )

    print(
        f"Step 5/5: done. Video saved as {video_path.name}",
        flush=True
    )

    return 0


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--episode",
        required=True
    )

    parser.add_argument(
        "--voice",
        default="true"
    )

    parser.add_argument(
        "--music",
        default="true"
    )

    parser.add_argument(
        "--out",
        default="output"
    )

    parser.add_argument(
        "--media-manifest",
        default="media-review/media-manifest.json"
    )

    args = parser.parse_args()

    try:
        return run(args)

    except ScriptError as error:
        print(
            f"\nERROR in your episode file: {error}"
        )
        return 2

    except Exception as error:
        print(
            f"\nERROR: {error}"
        )
        print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
