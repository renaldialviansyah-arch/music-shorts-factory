#!/usr/bin/env python3
"""Generate pending AI reconstructions for scenes with no archive candidates.

This script:
- never replaces or approves existing assets;
- skips scenes that already have an approved image;
- skips scenes that already have an AI reconstruction in the manifest;
- generates at most --max-images per run;
- makes one API request per scene, with no automatic retries;
- writes AI images under media/ai-reconstructions/<episode>/;
- marks generated assets as pending, never approved.

Requires GEMINI_API_KEY in the environment.
Uses the Gemini Interactions API and Python standard library only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_MODEL = "gemini-3.1-flash-lite-image"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
USER_AGENT = "MusicShortsFactory/1.0 (AI visual preparation)"


def read_episode(path: Path) -> tuple[str, list[dict[str, str]]]:
    raw = path.read_text(encoding="utf-8")
    title_match = re.search(r"(?im)^\s*TITLE\s*:\s*(.+)$", raw)
    title = title_match.group(1).strip() if title_match else path.stem
    scenes: list[dict[str, str]] = []

    for block in re.split(r"(?m)^\s*---\s*$", raw):
        item: dict[str, str] = {}
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


def safe_episode_stem(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    if not value:
        raise ValueError("Episode filename does not produce a safe folder name.")
    return value[:80]


def scene_has_approved_image(scene_entry: dict[str, Any]) -> bool:
    for asset in scene_entry.get("assets", []):
        if (
            str(asset.get("status", "")).lower() == "approved"
            and str(asset.get("media_type", "")).lower() == "image"
        ):
            return True
    return False


def scene_has_ai_image(scene_entry: dict[str, Any]) -> bool:
    for asset in scene_entry.get("assets", []):
        kind = str(asset.get("kind", "")).lower()
        source = str(asset.get("source_type", "")).lower()
        if kind == "ai_reconstruction" or source == "ai_reconstruction":
            return True
    return False


def make_prompt(title: str, scene: dict[str, str]) -> str:
    year = scene.get("year", "").strip()
    kicker = scene.get("kicker", "").strip()
    headline = scene.get("headline", "").replace("|", " ").strip()
    narration = scene.get("narration", "").strip()

    context = " | ".join(x for x in [year, kicker, headline, narration] if x)
    return f"""
Create one historically plausible cinematic documentary reconstruction image
for a music-history YouTube Short about: {title}.

SCENE CONTEXT:
{context}

VISUAL DIRECTION:
- Depict the scene described above, not a generic music collage.
- Era-appropriate objects, clothing, architecture, recording/radio equipment,
  interiors, and photographic details.
- 1970s British setting when the scene context indicates 1970s Britain.
- Photorealistic staged historical reconstruction, not a claimed authentic
  archival photograph.
- Warm amber tungsten lighting, deep natural shadows, subtle analog film grain,
  restrained vintage documentary color grading.
- Landscape 4:3 composition, medium-wide documentary framing, clear focal
  subject, realistic proportions and believable period details.
- No words, captions, dates, labels, logos, album covers, watermarks, or borders.
- Do not imitate or depict the recognizable face of a real person. If the
  narration names a real person, use a generic non-identifiable stand-in.
- Do not invent legible newspaper headlines, chart positions, record labels,
  or historical documents.
Return a single image only.
""".strip()


def call_gemini(prompt: str, api_key: str, model: str) -> tuple[bytes, str]:
    payload = {
        "model": model,
        "input": prompt,
        "response_format": {
            "type": "image",
            "mime_type": "image/png",
            "aspect_ratio": "4:3",
            "image_size": "1K",
        },
    }
    request = urllib.request.Request(
        API_URL,
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
        # Do not print request headers or API key.
        detail = exc.read(3000).decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini API returned HTTP {exc.code}: {detail}"
        ) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini API connection failed: {exc.reason}") from None

    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Gemini API returned an unreadable response.") from None

    output_image = result.get("output_image") or {}
    encoded = output_image.get("data")
    mime_type = str(output_image.get("mime_type") or "image/png").lower()

    if not encoded:
        # Surface a concise API message when no image was returned.
        message = result.get("error") or result.get("message") or "No output_image returned."
        raise RuntimeError(f"Gemini did not return an image: {message}")

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise RuntimeError("Gemini returned invalid base64 image data.") from None

    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise RuntimeError("Generated image is empty or exceeds the 15 MB safety limit.")

    signatures = {
        "image/png": image_bytes.startswith(bytes.fromhex("89504E470D0A1A0A")),
        "image/jpeg": image_bytes.startswith(bytes.fromhex("FFD8FF")),
    }
    if mime_type not in signatures or not signatures[mime_type]:
        raise RuntimeError(f"Generated image signature does not match MIME type {mime_type}.")

    return image_bytes, mime_type


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", required=True, help="Episode text file")
    parser.add_argument(
        "--manifest", default="media-review/media-manifest.json",
        help="Existing media manifest; updated in place without replacing approved assets",
    )
    parser.add_argument(
        "--output-root", default="media/ai-reconstructions",
        help="Directory for generated images",
    )
    parser.add_argument("--max-images", type=int, default=5)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    if not 1 <= args.max_images <= 10:
        print("ERROR: --max-images must be between 1 and 10.", file=sys.stderr)
        return 2

    episode_path = Path(args.episode)
    manifest_path = Path(args.manifest)
    if not episode_path.is_file():
        print(f"ERROR: episode file not found: {episode_path}", file=sys.stderr)
        return 2
    if not manifest_path.is_file():
        print(f"ERROR: media manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    try:
        title, scenes = read_episode(episode_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read episode or manifest: {exc}", file=sys.stderr)
        return 2

    episode_stem = safe_episode_stem(episode_path.stem)
    if manifest.get("episode") != episode_path.stem:
        print("ERROR: manifest episode does not match the selected episode.", file=sys.stderr)
        return 2

    manifest_scenes = {
        str(item.get("scene")): item
        for item in manifest.get("scenes", [])
        if isinstance(item, dict)
    }

    targets: list[tuple[dict[str, str], dict[str, Any]]] = []
    for scene in scenes:
        number = scene["scene"]
        entry = manifest_scenes.setdefault(number, {"scene": int(number), "assets": []})
        entry.setdefault("assets", [])
        if scene_has_approved_image(entry):
            print(f"Scene {number}: approved image exists; skipping AI generation.")
            continue
        if scene_has_ai_image(entry):
            print(f"Scene {number}: AI reconstruction already exists; skipping duplicate.")
            continue
        # Do not spend API budget when Wikimedia candidates still need review.
        if entry.get("assets"):
            print(
                f"Scene {number}: archive candidate(s) are awaiting review; "
                "skipping AI generation for now."
            )
            continue
        targets.append((scene, entry))

    if not targets:
        print("No eligible scenes need AI images. No API requests made.")
        return 0

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GEMINI_API_KEY secret is not configured.", file=sys.stderr)
        return 2

    if len(targets) > args.max_images:
        print(
            f"Eligible scenes: {len(targets)}; per-run cap: {args.max_images}. "
            "Only the first scenes will be generated."
        )
        targets = targets[:args.max_images]

    episode_dir = Path(args.output_root) / episode_stem
    episode_dir.mkdir(parents=True, exist_ok=True)
    generated = 0
    failed = 0

    for scene, entry in targets:
        number = int(scene["scene"])
        prompt = make_prompt(title, scene)
        try:
            image_bytes, mime_type = call_gemini(prompt, api_key, args.model)
            extension = ".png" if mime_type == "image/png" else ".jpg"
            filename = f"scene-{number:02d}{extension}"
            destination = episode_dir / filename
            destination.write_bytes(image_bytes)

            relative_path = destination.as_posix()
            asset = {
                "asset_id": f"scene-{number:02d}-ai-reconstruction",
                "status": "pending",
                "media_type": "image",
                "kind": "ai_reconstruction",
                "source_type": "ai_reconstruction",
                "storage": "repository",
                "local_path": relative_path,
                "title": f"AI historical reconstruction - scene {number}",
                "creator": "Generated with Google Gemini API",
                "license": "AI-generated; review applicable Google API terms",
                "model": args.model,
                "prompt": prompt,
                "sha256": "",
                "notes": (
                    "AI reconstruction, not an authentic archival photograph. "
                    "Review image and historical accuracy before approval. "
                    "Display label: AI RECONSTRUCTION."
                ),
            }
            entry["assets"].append(asset)
            generated += 1
            print(f"Scene {number}: generated pending AI image at {relative_path}.")
        except Exception as exc:
            failed += 1
            print(f"WARNING: Scene {number}: generation failed: {exc}", file=sys.stderr)
            # No retry; existing archive/SVG fallback remains available.

    # Preserve all pre-existing manifest fields and assets.
    manifest["scenes"] = sorted(
        manifest_scenes.values(),
        key=lambda item: int(item.get("scene", 0))
    )
    manifest.setdefault("policy", {})
    manifest["policy"]["ai_reconstructions_must_be_labelled"] = True
    manifest["policy"]["automatic_approval"] = False
    manifest["policy"]["only_approved_assets_render"] = True
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"AI generation complete: {generated} created, {failed} failed. "
        "All generated assets remain PENDING."
    )
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
