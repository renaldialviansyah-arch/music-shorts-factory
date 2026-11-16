#!/usr/bin/env python3
"""Reads the plain-text episode format and turns it into a Python dictionary.

You can also run it on its own to check a script without rendering:
    python scripts/parse_script.py episodes/my-episode.txt
"""
import json
import re
import sys

VALID_VISUALS = ("waveform", "vinyl", "rings", "spotlight", "timeline")
VALID_THEMES = ("amber", "crimson", "teal", "violet", "mono")
VALID_MOODS = ("minor", "major", "dreamy")
GLOBAL_KEYS = {"TITLE", "THEME", "MOOD", "CHANNEL", "HASHTAGS"}
SCENE_KEYS = {"VISUAL", "KICKER", "HEADLINE", "NARRATION", "YEAR", "SECONDS"}
MAX_QUOTE_WORDS = 8


class ScriptError(Exception):
    """A problem in the episode file that the user can fix."""


def _parse_block(block, allowed, where):
    data = {}
    current = None
    for line in block.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Z_]+)\s*:\s*(.*)$", stripped)
        if match:
            key, value = match.group(1), match.group(2).strip()
            if key not in allowed:
                raise ScriptError(
                    f"{where}: unknown key '{key}'. Allowed keys here: {', '.join(sorted(allowed))}."
                )
            data[key] = value
            current = key
        elif current is not None:
            # A wrapped line: add it to the previous key.
            data[current] = (data[current] + " " + stripped).strip()
        else:
            raise ScriptError(f"{where}: this line is not in 'KEY: value' form -> {stripped[:60]}")
    return data


def _check_text_safety(text, where):
    if "\u266a" in text or "\u266b" in text:
        raise ScriptError(f"{where}: music-note symbols suggest lyrics. Do not include song lyrics.")
    for match in re.finditer(r'["\u201c]([^"\u201d]+)["\u201d]', text):
        words = match.group(1).split()
        if len(words) > MAX_QUOTE_WORDS:
            raise ScriptError(
                f"{where}: a quoted passage is longer than {MAX_QUOTE_WORDS} words "
                f"({' '.join(words[:6])}...). It could be copyrighted lyrics. "
                "Paraphrase it in your own words instead."
            )


def parse_episode(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raise ScriptError(f"Episode file not found: {path}")
    raw = raw.replace("\r\n", "\n")
    blocks = re.split(r"(?m)^\s*-{3,}\s*$", raw)

    head = _parse_block(blocks[0], GLOBAL_KEYS, "Top of file")
    if not head.get("TITLE"):
        raise ScriptError("The top of the file needs a line like:  TITLE: My video title")

    theme = head.get("THEME", "amber").lower()
    mood = head.get("MOOD", "minor").lower()
    if theme not in VALID_THEMES:
        raise ScriptError(f"THEME must be one of: {', '.join(VALID_THEMES)} (you wrote '{theme}').")
    if mood not in VALID_MOODS:
        raise ScriptError(f"MOOD must be one of: {', '.join(VALID_MOODS)} (you wrote '{mood}').")

    scenes = []
    for index, block in enumerate(blocks[1:], start=1):
        if not block.strip() or all(
            (not ln.strip()) or ln.strip().startswith("#") for ln in block.split("\n")
        ):
            continue
        where = f"Scene {len(scenes) + 1}"
        data = _parse_block(block, SCENE_KEYS, where)
        narration = data.get("NARRATION", "").strip()
        if not narration:
            raise ScriptError(f"{where}: needs a NARRATION: line.")
        visual = data.get("VISUAL", "waveform").lower()
        if visual not in VALID_VISUALS:
            raise ScriptError(f"{where}: VISUAL must be one of: {', '.join(VALID_VISUALS)} (you wrote '{visual}').")
        seconds = None
        if data.get("SECONDS"):
            try:
                seconds = float(data["SECONDS"])
            except ValueError:
                raise ScriptError(f"{where}: SECONDS must be a number like 6 or 6.5.")
        for field in ("NARRATION", "HEADLINE", "KICKER"):
            _check_text_safety(data.get(field, ""), f"{where} {field}")
        scenes.append(
            {
                "visual": visual,
                "kicker": data.get("KICKER", ""),
                "headline": data.get("HEADLINE", ""),
                "narration": narration,
                "year": data.get("YEAR", ""),
                "seconds": seconds,
            }
        )

    if len(scenes) < 3:
        raise ScriptError(f"You need at least 3 scenes separated by lines of --- (found {len(scenes)}).")
    if len(scenes) > 14:
        raise ScriptError(f"Too many scenes ({len(scenes)}). Use 14 or fewer for a 30-60 second Short.")

    return {
        "title": head["TITLE"],
        "theme": theme,
        "mood": mood,
        "channel": head.get("CHANNEL", ""),
        "hashtags": head.get("HASHTAGS", "#musichistory #shorts"),
        "scenes": scenes,
    }


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/parse_script.py episodes/your-file.txt")
        return 2
    try:
        episode = parse_episode(sys.argv[1])
    except ScriptError as error:
        print(f"ERROR in your episode file: {error}")
        return 2
    words = sum(len(s["narration"].split()) for s in episode["scenes"])
    print(f"Script OK: {len(episode['scenes'])} scenes, {words} narration words.")
    if words < 70 or words > 150:
        print("Note: 90-125 narration words usually gives a 40-55 second Short.")
    print(json.dumps({"title": episode["title"], "theme": episode["theme"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
