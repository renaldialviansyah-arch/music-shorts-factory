#!/usr/bin/env python3
"""Merge fresh media search results without losing existing approvals."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Manifest not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Manifest root must be an object: {path}")

    return data


def scene_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}

    for scene in manifest.get("scenes", []):
        if not isinstance(scene, dict):
            continue

        number = str(scene.get("scene", "")).strip()
        if not number:
            continue

        scene.setdefault("assets", [])
        result[number] = scene

    return result


def asset_identity(asset: dict[str, Any]) -> str:
    source = str(asset.get("source_page", "")).strip()
    if source:
        return "source:" + source

    local_path = str(asset.get("local_path", "")).strip()
    if local_path:
        return "local:" + local_path

    asset_id = str(asset.get("asset_id", "")).strip()
    if asset_id:
        return "id:" + asset_id

    download_url = str(asset.get("download_url", "")).strip()
    if download_url:
        return "url:" + download_url

    return ""


def merge_assets(
    existing: list[Any],
    incoming: list[Any]
) -> tuple[list[dict[str, Any]], int]:

    merged = []
    known = set()
    added = 0

    # Existing assets come first. This preserves previous approvals
    # and pending decisions rather than replacing them with new results.
    for asset in existing + incoming:
        if not isinstance(asset, dict):
            continue

        identity = asset_identity(asset)

        if identity and identity in known:
            continue

        if identity:
            known.add(identity)

        merged.append(asset)

        if asset in incoming:
            added += 1

    return merged, added


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--existing",
        default="media-review/media-manifest.json"
    )
    parser.add_argument(
        "--incoming",
        default="media-review-new/media-manifest.json"
    )
    parser.add_argument(
        "--output",
        default="media-review/media-manifest.json"
    )
    args = parser.parse_args()

    existing_path = Path(args.existing)
    incoming_path = Path(args.incoming)
    output_path = Path(args.output)

    try:
        existing = read_json(existing_path)
        incoming = read_json(incoming_path)

        old_episode = str(existing.get("episode", "")).strip()
        new_episode = str(incoming.get("episode", "")).strip()

        if not old_episode or old_episode != new_episode:
            raise ValueError(
                "Existing and incoming manifests belong to "
                "different episodes. Nothing was merged."
            )

        old_scenes = scene_map(existing)
        new_scenes = scene_map(incoming)

        added_assets = 0

        for number, new_scene in new_scenes.items():
            old_scene = old_scenes.get(number)

            if old_scene is None:
                old_scenes[number] = new_scene
                added_assets += len(new_scene.get("assets", []))
                continue

            merged, added = merge_assets(
                old_scene.get("assets", []),
                new_scene.get("assets", [])
            )

            old_scene["assets"] = merged
            added_assets += added

            # Retain useful search metadata without overwriting
            # manually maintained approval or asset information.
            for key in (
                "visual_intent",
                "search_query",
                "search_attempts"
            ):
                if new_scene.get(key):
                    old_scene[key] = new_scene[key]

        result = dict(existing)

        for key in ("schema_version", "title", "search_mode"):
            if incoming.get(key) is not None:
                result[key] = incoming[key]

        result["episode"] = old_episode
        result["scenes"] = sorted(
            old_scenes.values(),
            key=lambda item: int(item.get("scene", 0))
        )

        policy = dict(existing.get("policy", {}))
        policy.update(incoming.get("policy", {}))
        policy["automatic_approval"] = False
        policy["only_approved_assets_render"] = True
        policy["ai_reconstructions_must_be_labelled"] = True
        result["policy"] = policy

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )

        print(f"Manifest merged successfully: {output_path}")
        print(f"Scenes retained: {len(result['scenes'])}")
        print(f"New assets added: {added_assets}")
        print("Existing assets and approval statuses were preserved.")
        print("Automatic approval remains disabled.")

        return 0

    except (OSError, ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
  
