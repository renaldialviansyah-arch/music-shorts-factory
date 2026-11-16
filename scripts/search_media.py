#!/usr/bin/env python3
"""Search Wikimedia Commons for reviewable media candidates from an episode script.

This tool only creates a review bundle. It does not approve assets, modify audio,
or render a video. Only manually approved assets may be used by later stages.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "MusicShortsFactoryMediaReview/1.0 (GitHub Actions; educational project)"
MAX_CANDIDATES = 3


def read_episode(path: Path) -> tuple[str, list[dict[str, str]]]:
    """Read the existing episode-script format without changing the script."""
    text = path.read_text(encoding="utf-8")

    title_match = re.search(r"(?im)^\s*TITLE\s*:\s*(.+)$", text)
    title = title_match.group(1).strip() if title_match else path.stem

    scenes: list[dict[str, str]] = []
    blocks = re.split(r"(?m)^\s*---\s*$", text)

    for block in blocks:
        scene: dict[str, str] = {}

        for raw in block.splitlines():
            line = raw.strip()

            if not line or line.startswith("#") or ":" not in line:
                continue

            key, value = line.split(":", 1)
            key = key.strip().upper()

            if key in {"VISUAL", "KICKER", "HEADLINE", "NARRATION", "YEAR"}:
                scene[key.lower()] = value.strip()

        if scene and any(
            scene.get(key) for key in ("kicker", "headline", "narration")
        ):
            scene["scene"] = str(len(scenes) + 1)
            scenes.append(scene)

    return title, scenes


def infer_scene_intent(scene: dict[str, str]) -> tuple[str, list[str]]:
    """Infer a broad visual subject from the scene's actual content."""
    text = " ".join(
        [
            scene.get("kicker", ""),
            scene.get("headline", ""),
            scene.get("narration", ""),
        ]
    ).lower()

    rules = [
        (
            "radio_broadcast",
            [
                "radio", "dj ", "disc jockey", "airplay", "broadcaster",
                "broadcast", "radio host", "presenter",
            ],
            ["radio studio", "radio DJ", "microphone"],
        ),
        (
            "audience_demand",
            [
                "listeners", "listener", "audience", "fans", "fan ",
                "requested", "request", "demand", "crowd",
            ],
            ["music listeners", "music fans", "radio audience"],
        ),
        (
            "music_charts",
            [
                "number one", "number 1", "charts", "chart", "rank",
                "weeks at", "sales", "top ten", "top 10",
            ],
            ["music charts", "record sales", "music ranking"],
        ),
        (
            "music_release",
            ["released", "release", "single", "vinyl", "record pressing"],
            ["vinyl record", "music single", "record release"],
        ),
        (
            "recording_studio",
            [
                "recording", "recorded", "studio", "producer",
                "track", "album session",
            ],
            ["recording studio", "music production", "audio mixing"],
        ),
        (
            "live_performance",
            [
                "concert", "performed", "performing", "live show",
                "on stage", "performance",
            ],
            ["live music", "rock concert", "band performance"],
        ),
    ]

    for name, keywords, search_terms in rules:
        if any(keyword in text for keyword in keywords):
            return name, search_terms

    if any(word in text for word in ["follow", "subscribe"]):
        return "music_documentary_closing", [
            "music history", "rock music", "music archive",
        ]

    return "music_history_archive", [
        "music history", "rock music", "historical music archive",
    ]


def make_query(
    scene: dict[str, str],
    episode_title: str,
    all_scenes: list[dict[str, str]] | None = None,
) -> str:
    """Build a concise query with episode identity and scene-specific intent."""
    stop = {
        "the", "and", "for", "with", "from", "that", "this",
        "was", "were", "are", "his", "her", "their", "they",
        "them", "into", "over", "under", "when", "where",
        "which", "while", "about", "after", "before", "then",
        "but", "not", "you", "your", "its", "has", "had",
        "have", "who", "in", "on", "at", "to", "of", "a",
        "an", "it", "as", "is", "be", "by", "or", "nearly",
        "twice", "long", "more", "wanted",
    }

    def clean_words(value: str) -> list[str]:
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", value)
        result: list[str] = []
        for word in words:
            if word.lower() not in stop and word.lower() not in [
                item.lower() for item in result
            ]:
                result.append(word)
        return result

    opening = all_scenes[0] if all_scenes else scene

    context_text = " ".join(
        [
            episode_title,
            opening.get("kicker", ""),
            opening.get("headline", ""),
            opening.get("year", ""),
        ]
    )
    context = clean_words(context_text)

    scene_text = " ".join(
        [
            scene.get("headline", ""),
            scene.get("kicker", ""),
            scene.get("narration", ""),
        ]
    )
    scene_words = clean_words(scene_text)
    _intent_name, intent_terms = infer_scene_intent(scene)

    selected: list[str] = []

    # Keep the episode identity at the beginning.
    for word in context:
        if word.lower() not in [item.lower() for item in selected]:
            selected.append(word)

    # Add scene-specific terms before generic intent terms.
    for word in scene_words:
        if word.lower() not in [item.lower() for item in selected]:
            selected.append(word)

    for phrase in intent_terms:
        for word in clean_words(phrase):
            if word.lower() not in [item.lower() for item in selected]:
                selected.append(word)

    return " ".join(selected[:12]) or episode_title


def request_json(params: dict[str, str]) -> dict[str, Any]:
    url = API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def commons_search(query: str) -> list[dict[str, Any]]:
    """Search Commons with progressively broader query variants."""

    def search_once(search_query: str) -> list[dict[str, Any]]:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {search_query}",
            "gsrnamespace": "6",
            "gsrlimit": str(MAX_CANDIDATES),
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": "640",
        }

        data = request_json(params)
        pages = (data.get("query") or {}).get("pages") or {}
        results: list[dict[str, Any]] = []

        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            metadata = info.get("extmetadata") or {}

            def meta_value(name: str) -> str:
                value = metadata.get(name, {}).get("value", "")
                return html.unescape(
                    re.sub(r"<[^>]+>", "", str(value))
                ).strip()

            results.append(
                {
                    "title": page.get("title", ""),
                    "preview_url": info.get("thumburl") or info.get("url", ""),
                    "download_url": info.get("url", ""),
                    "source_page": info.get("descriptionurl", ""),
                    "creator": meta_value("Artist"),
                    "license": meta_value("LicenseShortName"),
                    "license_url": meta_value("LicenseUrl"),
                    "attribution": meta_value("Attribution"),
                    "media_type": "image",
                    "provider": "Wikimedia Commons",
                }
            )

        return results

    words = query.split()
    no_year = [
        word for word in words
        if not re.fullmatch(r"(18|19|20)\d{2}", word)
    ]

    variants = [
        query,
        " ".join(no_year[:6]),
        " ".join(no_year[:4]),
        " ".join(no_year[:3]),
    ]

    unique_variants: list[str] = []
    for variant in variants:
        variant = variant.strip()
        if variant and variant.lower() not in [
            item.lower() for item in unique_variants
        ]:
            unique_variants.append(variant)

    results: list[dict[str, Any]] = []
    seen_sources: set[str] = set()

    for search_query in unique_variants:
        try:
            candidates = search_once(search_query)
        except Exception as exc:
            print(
                f"Search failed for '{search_query}': {exc}",
                file=sys.stderr,
            )
            continue

        for item in candidates:
            source = item.get("source_page", "")
            if source and source not in seen_sources:
                seen_sources.add(source)
                item["matched_query"] = search_query
                results.append(item)

        if len(results) >= MAX_CANDIDATES:
            break

        time.sleep(0.2)

    return results[:MAX_CANDIDATES]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--episode",
        required=True,
        help="Path to episode script",
    )
    parser.add_argument(
        "--output",
        default="media-review",
        help="Review output directory",
    )
    args = parser.parse_args()

    episode_path = Path(args.episode)
    if not episode_path.is_file():
        print(
            f"Episode script not found: {episode_path}",
            file=sys.stderr,
        )
        return 2

    title, scenes = read_episode(episode_path)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    report = [
        f"# Media review - {title}",
        "",
        "> Review required. Search results are candidates, not approved assets.",
        "> Check each original file page and its current license before use.",
        "",
    ]

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "episode": episode_path.stem,
        "title": title,
        "policy": {
            "only_approved_assets_render": True,
            "unknown_license_allowed": False,
            "ai_reconstructions_must_be_labelled": True,
        },
        "scenes": [],
    }

    # One set for the whole episode: the same Commons file will not be
    # suggested again in a later scene.
    seen_sources: set[str] = set()

    for scene in scenes:
        scene_no = scene.get("scene", str(len(manifest["scenes"]) + 1))
        query = make_query(scene, title, scenes)

        report.extend(
            [
                f"## Scene {scene_no}",
                f"- Search query: `{query}`",
                "",
            ]
        )
        scene_assets: list[dict[str, Any]] = []

        try:
            candidates = commons_search(query)
        except Exception as exc:
            candidates = []
            report.append(
                f"- Search error: `{type(exc).__name__}: {exc}`"
            )

        # Remove assets already used by earlier scenes in this episode.
        candidates = [
            item for item in candidates
            if item.get("source_page")
            and item.get("source_page") not in seen_sources
        ]

        if not candidates:
            report.append(
                "- No new unique candidates found. "
                "Choose a manual visual or try another search."
            )

        for idx, item in enumerate(candidates, start=1):
            asset_id = f"scene-{scene_no.zfill(2)}-candidate-{idx}"
            source = item.get("source_page", "")
            seen_sources.add(source)

            item_row = {
                "scene": scene_no,
                "asset_id": asset_id,
                "query": query,
                "title": item.get("title", ""),
                "provider": item.get("provider", ""),
                "media_type": item.get("media_type", ""),
                "preview_url": item.get("preview_url", ""),
                "download_url": item.get("download_url", ""),
                "source_page": source,
                "creator": item.get("creator", ""),
                "license": item.get("license", ""),
                "license_url": item.get("license_url", ""),
                "attribution": item.get("attribution", ""),
                "status": "pending",
            }
            rows.append(item_row)

            scene_assets.append(
                {
                    "asset_id": asset_id,
                    "status": "pending",
                    "media_type": item.get("media_type", "image"),
                    "kind": "historical_archive",
                    "storage": "runtime",
                    "download_url": item.get("download_url", ""),
                    "source_page": source,
                    "creator": item.get("creator", ""),
                    "license": item.get("license", ""),
                    "license_url": item.get("license_url", ""),
                    "attribution": item.get("attribution", ""),
                    "sha256": "",
                    "notes": (
                        "Candidate only. Verify source page and license; "
                        "do not render while pending."
                    ),
                }
            )

            report.extend(
                [
                    f"### Candidate {idx}: {item.get('title', 'Untitled')}",
                    f"- Source: {source}",
                    f"- Creator: {item.get('creator') or 'Not listed'}",
                    (
                        "- License: "
                        f"{item.get('license') or 'Not listed - verify manually'}"
                    ),
                    (
                        "- License details: "
                        f"{item.get('license_url') or 'Not listed'}"
                    ),
                    f"- Preview: {item.get('preview_url', '')}",
                    "- Status: **PENDING - NOT APPROVED**",
                    "",
                ]
            )

        manifest["scenes"].append(
            {
                "scene": int(scene_no) if scene_no.isdigit() else scene_no,
                "search_query": query,
                "assets": scene_assets,
            }
        )
        time.sleep(0.2)

    with (out / "media-review.md").open("w", encoding="utf-8") as file:
        file.write("\n".join(report))

    with (out / "media-manifest.csv").open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        fields = list(rows[0].keys()) if rows else [
            "scene",
            "asset_id",
            "query",
            "title",
            "provider",
            "media_type",
            "preview_url",
            "download_url",
            "source_page",
            "creator",
            "license",
            "license_url",
            "attribution",
            "status",
        ]
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    (out / "media-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Review bundle created in: {out}")
    print(f"Scenes: {len(scenes)} | Candidates: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
