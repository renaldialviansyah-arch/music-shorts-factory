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
import os
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
    text = path.read_text(encoding="utf-8")
    title_match = re.search(r"(?im)^\s*TITLE\s*:\s*(.+)$", text)
    title = title_match.group(1).strip() if title_match else path.stem
    scenes: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"(?i)^SCENE\s+\d+", line):
            if current:
                scenes.append(current)
            current = {"scene": re.search(r"\d+", line).group(0)}
            continue
        if current and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().upper()
            if key in {"KICKER", "HEADLINE", "NARRATION", "YEAR", "VISUAL"}:
                current[key.lower()] = value.strip()
    if current:
        scenes.append(current)
    if not scenes:
        # Fallback: extract scene-like blocks using common headings.
        blocks = re.split(r"(?im)^\s*SCENE\s+\d+.*$", text)
        for idx, block in enumerate(blocks[1:], start=1):
            cleaned = re.sub(r"\s+", " ", block).strip()
            if cleaned:
                scenes.append({"scene": str(idx), "narration": cleaned[:500]})
    return title, scenes


def make_query(scene: dict[str, str], episode_title: str) -> str:
    parts = []
    for key in ("headline", "kicker", "year", "narration"):
        value = scene.get(key, "")
        if value:
            parts.append(value)
    raw = " ".join(parts) or episode_title
    raw = re.sub(r"https?://\S+", " ", raw)
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", raw)
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "was", "were",
        "are", "his", "her", "their", "they", "them", "into", "over", "under",
        "when", "where", "which", "while", "about", "after", "before", "then",
        "but", "not", "you", "your", "its", "has", "had", "have", "who",
    }
    chosen = []
    for word in words:
        if word.lower() not in stop and word.lower() not in {x.lower() for x in chosen}:
            chosen.append(word)
        if len(chosen) >= 9:
            break
    return " ".join(chosen) or episode_title


def request_json(params: dict[str, str]) -> dict[str, Any]:
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def commons_search(query: str) -> list[dict[str, Any]]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6",
        "gsrlimit": str(MAX_CANDIDATES),
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": "640",
    }
    data = request_json(params)
    pages = (data.get("query") or {}).get("pages") or {}
    results = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        def meta_value(name: str) -> str:
            value = meta.get(name, {}).get("value", "")
            return html.unescape(re.sub(r"<[^>]+>", "", str(value))).strip()
        results.append({
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
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", required=True, help="Path to episode script")
    parser.add_argument("--output", default="media-review", help="Review output directory")
    args = parser.parse_args()
    episode_path = Path(args.episode)
    if not episode_path.is_file():
        print(f"Episode script not found: {episode_path}", file=sys.stderr)
        return 2

    title, scenes = read_episode(episode_path)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    report = [
        f"# Media review 鈥� {title}", "",
        "> Review required. Search results are candidates, not approved assets.",
        "> Check each original file page and its current license before use.", "",
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

    for scene in scenes:
        scene_no = scene.get("scene", str(len(manifest["scenes"]) + 1))
        query = make_query(scene, title)
        report.extend([f"## Scene {scene_no}", f"- Search query: `{query}`", ""])
        scene_assets = []
        try:
            candidates = commons_search(query)
        except Exception as exc:
            candidates = []
            report.append(f"- Search error: `{type(exc).__name__}: {exc}`")
        if not candidates:
            report.append("- No candidates found. Choose a manual visual or try a better query.")
        for idx, item in enumerate(candidates, start=1):
            asset_id = f"scene-{scene_no.zfill(2)}-candidate-{idx}"
            item_row = {
                "scene": scene_no,
                "asset_id": asset_id,
                "query": query,
                "title": item["title"],
                "provider": item["provider"],
                "media_type": item["media_type"],
                "preview_url": item["preview_url"],
                "download_url": item["download_url"],
                "source_page": item["source_page"],
                "creator": item["creator"],
                "license": item["license"],
                "license_url": item["license_url"],
                "attribution": item["attribution"],
                "status": "pending",
            }
            rows.append(item_row)
            scene_assets.append({
                "asset_id": asset_id,
                "status": "pending",
                "media_type": item["media_type"],
                "kind": "historical_archive",
                "storage": "runtime",
                "download_url": item["download_url"],
                "source_page": item["source_page"],
                "creator": item["creator"],
                "license": item["license"],
                "license_url": item["license_url"],
                "attribution": item["attribution"],
                "sha256": "",
                "notes": "Candidate only. Verify source page and license; do not render while pending.",
            })
            report.extend([
                f"### Candidate {idx}: {item['title']}",
                f"- Source: {item['source_page']}",
                f"- Creator: {item['creator'] or 'Not listed'}",
                f"- License: {item['license'] or 'Not listed 鈥� verify manually'}",
                f"- License details: {item['license_url'] or 'Not listed'}",
                f"- Preview: {item['preview_url']}",
                "- Status: **PENDING 鈥� NOT APPROVED**", "",
            ])
        manifest["scenes"].append({
            "scene": int(scene_no) if scene_no.isdigit() else scene_no,
            "search_query": query,
            "assets": scene_assets,
        })
        time.sleep(0.2)

    with (out / "media-review.md").open("w", encoding="utf-8") as f:
        f.write("\n".join(report))
    with (out / "media-manifest.csv").open("w", newline="", encoding="utf-8") as f:
        fields = list(rows[0].keys()) if rows else [
            "scene", "asset_id", "query", "title", "provider", "media_type",
            "preview_url", "download_url", "source_page", "creator", "license",
            "license_url", "attribution", "status"
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (out / "media-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Review bundle created in: {out}")
    print(f"Scenes: {len(scenes)} | Candidates: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
