
#!/usr/bin/env python3
"""Generate pending AI reconstructions for eligible music documentary scenes."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_URL = (
    "https://generativelanguage.googleapis.com/v1/"
    "models/{model}:generateContent"
)
DEFAULT_MODEL = "gemini-3.1-flash-lite-image"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
USER_AGENT = "MusicShortsFactory/1.0"


def read_episode(path: Path):
    raw = path.read_text(encoding="utf-8")
    match = re.search(r"(?im)^\s*TITLE\s*:\s*(.+)$", raw)
    title = match.group(1).strip() if match else path.stem
    scenes = []

    for block in re.split(r"(?m)^\s*---\s*$", raw):
        item = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().upper()
            if key in {"VISUAL", "KICKER", "HEADLINE", "NARRATION", "YEAR"}:
                item[key.lower()] = value.strip()

        if item and any(item.get(k) for k in ("kicker", "headline", "narration")):
            item["scene"] = str(len(scenes) + 1)
            scenes.append(item)

    return title, scenes


def safe_stem(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    if not result:
        raise ValueError("Invalid episode folder name.")
    return result[:80]


def has_approved_image(entry: dict[str, Any]) -> bool:
    return any(
        str(a.get("status", "")).lower() == "approved"
        and str(a.get("media_type", "")).lower() == "image"
        for a in entry.get("assets", [])
    )


def has_ai_image(entry: dict[str, Any]) -> bool:
    return any(
        a.get("kind") == "ai_reconstruction"
        or a.get("source_type") == "ai_reconstruction"
        for a in entry.get("assets", [])
    )


def has_pending_archive_candidate(entry: dict[str, Any]) -> bool:
    return any(
        str(a.get("status", "")).lower() == "pending"
        and a.get("kind") != "ai_reconstruction"
        and a.get("source_type") != "ai_reconstruction"
        for a in entry.get("assets", [])
    )


def make_prompt(title: str, scene: dict[str, str]) -> str:
    context = " | ".join(
        value for value in (
            scene.get("year", ""),
            scene.get("kicker", ""),
            scene.get("headline", "").replace("|", " "),
            scene.get("narration", ""),
            scene.get("visual", ""),
        ) if value
    )

    return f"""
Create one historically plausible cinematic documentary reconstruction
image for a music-history YouTube Short about: {title}.

SCENE CONTEXT:
{context}

VISUAL DIRECTION:
- Depict the specific scene, not a generic music collage.
- Use era-appropriate objects, clothing, architecture and equipment.
- Photorealistic staged historical reconstruction.
- Never present the image as an authentic archival photograph.
- Warm amber tungsten lighting, deep natural shadows and subtle analog film grain.
- Landscape 4:3 composition, medium-wide documentary framing.
- No words, captions, dates, labels, logos, album covers, watermarks or borders.
- Do not imitate recognizable faces of real people.
- Use generic non-identifiable people when a real person is mentioned.
- Do not invent readable historical documents or newspaper headlines.
Return one image only.
""".strip()


def call_gemini(prompt: str, api_key: str, model: str):
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "responseFormat": {
                "image": {
                    "aspectRatio": "4:3",
                    "imageSize": "1K"
                }
            }
        }
    }

    request = urllib.request.Request(
        API_URL.format(model=model),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini connection failed: {exc.reason}") from None

    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Gemini returned an unreadable response.") from None

    image_part = None
    for candidate in result.get("candidates", []):
        content = candidate.get("content") or {}
        for part in content.get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                image_part = inline
                break
        if image_part:
            break

    if not image_part:
        feedback = result.get("promptFeedback") or {}
        reason = feedback.get("blockReason")
        if reason:
            raise RuntimeError(f"Gemini blocked the prompt: {reason}")
        raise RuntimeError("Gemini response contained no image.")

    mime = str(
        image_part.get("mimeType")
        or image_part.get("mime_type")
        or ""
    ).lower()

    try:
        data = base64.b64decode(image_part["data"], validate=True)
    except (ValueError, binascii.Error, KeyError):
        raise RuntimeError("Gemini returned invalid image data.") from None

    if not data or len(data) > MAX_IMAGE_BYTES:
        raise RuntimeError("Image is empty or exceeds the 15 MB limit.")

    signatures = {
        "image/png": data.startswith(bytes.fromhex("89504E470D0A1A0A")),
        "image/jpeg": data.startswith(bytes.fromhex("FFD8FF")),
    }

    if mime not in signatures or not signatures[mime]:
        raise RuntimeError(f"Unexpected image format: {mime!r}")

    return data, mime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", required=True)
    parser.add_argument(
        "--manifest",
        default="media-review/media-manifest.json"
    )
    parser.add_argument(
        "--output-root",
        default="media/ai-reconstructions"
    )
    parser.add_argument("--max-images", type=int, default=5)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    if not 1 <= args.max_images <= 10:
        print("ERROR: max-images must be between 1 and 10.", file=sys.stderr)
        return 2

    episode_path = Path(args.episode)
    manifest_path = Path(args.manifest)

    if not episode_path.is_file() or not manifest_path.is_file():
        print("ERROR: episode or manifest file not found.", file=sys.stderr)
        return 2

    try:
        title, scenes = read_episode(episode_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read input: {exc}", file=sys.stderr)
        return 2

    if manifest.get("episode") != episode_path.stem:
        print("ERROR: manifest episode does not match script.", file=sys.stderr)
        return 2

    scene_map = {
        str(item.get("scene")): item
        for item in manifest.get("scenes", [])
        if isinstance(item, dict)
    }

    targets = []

    for scene in scenes:
        number = scene["scene"]
        entry = scene_map.setdefault(
            number, {"scene": int(number), "assets": []}
        )
        entry.setdefault("assets", [])

        if has_approved_image(entry):
            print(f"Scene {number}: approved image exists; skipping.")
            continue

        if has_ai_image(entry):
            print(f"Scene {number}: AI image already exists; skipping.")
            continue

        if has_pending_archive_candidate(entry):
            print(f"Scene {number}: archive candidate pending review; skipping.")
            continue

        targets.append((scene, entry))

    if not targets:
        print("No eligible scenes. No API requests made.")
        return 0

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not configured.", file=sys.stderr)
        return 2

    if len(targets) > args.max_images:
        print(
            f"{len(targets)} eligible scenes; limiting this run "
            f"to {args.max_images} images."
        )
        targets = targets[:args.max_images]

    episode_dir = Path(args.output_root) / safe_stem(episode_path.stem)
    episode_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    failed = 0

    for scene, entry in targets:
        number = int(scene["scene"])
        prompt = make_prompt(title, scene)

        try:
            image_data, mime = call_gemini(prompt, api_key, args.model)
            extension = ".png" if mime == "image/png" else ".jpg"
            destination = episode_dir / f"scene-{number:02d}{extension}"

            if destination.exists():
                print(f"Scene {number}: file already exists; skipping.")
                continue

            destination.write_bytes(image_data)
            digest = hashlib.sha256(image_data).hexdigest()

            entry["assets"].append({
                "asset_id": f"scene-{number:02d}-ai-reconstruction",
                "status": "pending",
                "media_type": "image",
                "kind": "ai_reconstruction",
                "source_type": "ai_reconstruction",
                "storage": "repository",
                "local_path": destination.as_posix(),
                "title": f"AI historical reconstruction - scene {number}",
                "creator": "Generated with Google Gemini API",
                "license": "AI-generated; review applicable Google API terms",
                "model": args.model,
                "prompt": prompt,
                "sha256": digest,
                "notes": (
                    "AI reconstruction, not an authentic archival photograph. "
                    "Review historical accuracy before approval. "
                    "Display label: AI RECONSTRUCTION."
                ),
            })

            generated += 1
            print(f"Scene {number}: generated PENDING image: {destination}")

        except Exception as exc:
            failed += 1
            print(
                f"WARNING: Scene {number} failed: {exc}",
                file=sys.stderr
            )

    manifest["scenes"] = sorted(
        scene_map.values(),
        key=lambda item: int(item.get("scene", 0))
    )
    manifest.setdefault("policy", {})
    manifest["policy"]["ai_reconstructions_must_be_labelled"] = True
    manifest["policy"]["automatic_approval"] = False
    manifest["policy"]["only_approved_assets_render"] = True

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    print(
        f"Finished: {generated} generated, {failed} failed. "
        "All generated images remain PENDING."
    )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
