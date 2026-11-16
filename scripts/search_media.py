#!/usr/bin/env python3
"""Scene-aware Wikimedia Commons media search with relevance and duplicate control.

Creates a review bundle only. It never approves assets, changes audio, or renders video.
Only manually approved assets may be used by later stages.
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
USER_AGENT = "MusicShortsFactoryMediaReview/2.0 (GitHub Actions; educational project)"
MAX_CANDIDATES = 3
MIN_RELEVANCE_SCORE = 3
REQUEST_DELAY = 0.25

STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "were",
    "are", "his", "her", "their", "they", "them", "into", "over", "under",
    "when", "where", "which", "while", "about", "after", "before", "then",
    "but", "not", "you", "your", "its", "has", "had", "have", "who", "in",
    "on", "at", "to", "of", "a", "an", "it", "as", "is", "be", "by", "or",
    "nearly", "twice", "long", "more", "wanted", "released", "release",
    "story", "stories", "music", "song", "episode", "follow", "subscribe",
}

# Scene intent -> concrete visual concepts. Keep these generic so they work
# across music-history episodes, not just one artist or song.
INTENT_RULES = [
    ("radio_broadcast",
     ["radio", "dj", "disc jockey", "airplay", "broadcaster", "broadcast",
      "radio host", "presenter", "radio station", "played on radio"],
     ["radio studio", "radio broadcasting", "radio presenter microphone",
      "vintage radio equipment"]),
    ("audience_demand",
     ["listeners", "listener", "audience", "fans", "fan", "requested",
      "request", "demand", "crowd", "asking for"],
     ["concert audience", "music fans crowd", "people listening to records",
      "radio audience"]),
    ("music_charts",
     ["number one", "number 1", "charts", "chart", "rank", "weeks at",
      "sales", "top ten", "top 10", "hit parade"],
     ["music charts", "record chart billboard", "music hit parade",
      "record sales chart"]),
    ("music_release",
     ["released", "release", "single", "vinyl", "record pressing",
      "record store", "on sale"],
     ["vinyl record single", "45 rpm record", "record store vinyl",
      "vintage vinyl record"]),
    ("recording_studio",
     ["recording", "recorded", "studio", "producer", "track",
      "album session", "tape", "mixing"],
     ["recording studio", "audio mixing console", "reel to reel tape",
      "music producer studio"]),
    ("live_performance",
     ["concert", "performed", "performing", "live show", "on stage",
      "performance", "tour", "gig"],
     ["rock concert stage", "live band performance", "concert crowd",
      "rock band on stage"]),
    ("songwriting",
     ["wrote", "written", "writing", "composed", "composition",
      "lyrics", "piano", "guitar riff"],
     ["songwriter piano", "musician composing music", "handwritten music sheet",
      "piano guitar songwriting"]),
    ("band_history",
     ["band", "formed", "group", "members", "guitarist", "singer",
      "drummer", "bassist"],
     ["rock band archival photograph", "rock musicians group portrait",
      "band performing on stage"]),
]

INTENT_FALLBACKS = {
    "music_documentary_closing": [
        "music archive photograph", "vintage record player", "rock concert audience"
    ],
    "music_history_archive": [
        "historical music archive", "vintage record player", "rock music history"
    ],
}


def read_episode(path: Path) -> tuple[str, list[dict[str, str]]]:
    text = path.read_text(encoding="utf-8")
    title_match = re.search(r"(?im)^\s*TITLE\s*:\s*(.+)$", text)
    title = title_match.group(1).strip() if title_match else path.stem
    scenes: list[dict[str, str]] = []

    for block in re.split(r"(?m)^\s*---\s*$", text):
        scene: dict[str, str] = {}
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().upper()
            if key in {"VISUAL", "KICKER", "HEADLINE", "NARRATION", "YEAR"}:
                scene[key.lower()] = value.strip()
        if scene and any(scene.get(k) for k in ("kicker", "headline", "narration")):
            scene["scene"] = str(len(scenes) + 1)
            scenes.append(scene)
    return title, scenes


def words(value: str) -> list[str]:
    found = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", value.lower())
    return [w for w in found if w not in STOP_WORDS and len(w) > 1]


def infer_scene_intent(scene: dict[str, str]) -> tuple[str, list[str]]:
    text = " ".join(scene.get(k, "") for k in ("kicker", "headline", "narration")).lower()
    for name, keywords, queries in INTENT_RULES:
        if any(k in text for k in keywords):
            return name, queries
    if any(k in text for k in ("follow", "subscribe")):
        return "music_documentary_closing", INTENT_FALLBACKS["music_documentary_closing"]
    return "music_history_archive", INTENT_FALLBACKS["music_history_archive"]


def extract_episode_identity(title: str, scenes: list[dict[str, str]]) -> list[str]:
    # Use opening-scene names/identifiers, not the whole episode title.
    opening = scenes[0] if scenes else {}
    text = " ".join([opening.get("kicker", ""), opening.get("headline", "")])
    candidates = words(text)
    # Retain likely proper names from title/opening while excluding generic terms.
    title_words = words(title)
    identity: list[str] = []
    for token in candidates + title_words:
        if token not in identity and len(token) > 2:
            identity.append(token)
    return identity[:5]


def make_queries(
    scene: dict[str, str],
    episode_title: str,
    all_scenes: list[dict[str, str]],
) -> tuple[str, list[str], str]:
    intent_name, intent_queries = infer_scene_intent(scene)
    identity = extract_episode_identity(episode_title, all_scenes)

    local_text = " ".join([
        scene.get("kicker", ""), scene.get("headline", ""),
        scene.get("narration", "")
    ])
    local_terms = []
    for token in words(local_text):
        if token not in local_terms:
            local_terms.append(token)

    # Scene terms lead. Episode identity is a supporting context, never the
    # entire query. Several alternatives broaden search without narration noise.
    scene_core = local_terms[:5]
    identity_text = " ".join(identity[:3])
    scene_text = " ".join(scene_core[:4])
    queries: list[str] = []

    if scene_text and identity_text:
        queries.append(f"{scene_text} {identity_text}")
    if scene_text:
        queries.append(scene_text)
    for fallback in intent_queries[:3]:
        queries.append(f"{fallback} {identity_text}".strip())

    # Deduplicate query variants, retain order, and keep them short.
    unique: list[str] = []
    seen = set()
    for query in queries:
        query = " ".join(query.split())[:110].strip()
        key = query.lower()
        if query and key not in seen:
            seen.add(key)
            unique.append(query)
    return (unique[0] if unique else episode_title), unique[:5], intent_name


def request_json(params: dict[str, str]) -> dict[str, Any]:
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def strip_html(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()


def commons_search(query: str) -> list[dict[str, Any]]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6",
        "gsrlimit": "12",
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
            return strip_html((meta.get(name) or {}).get("value", ""))

        source = info.get("descriptionurl", "")
        if not source:
            continue
        results.append({
            "title": page.get("title", ""),
            "preview_url": info.get("thumburl") or info.get("url", ""),
            "download_url": info.get("url", ""),
            "source_page": source,
            "creator": meta_value("Artist"),
            "license": meta_value("LicenseShortName"),
            "license_url": meta_value("LicenseUrl"),
            "attribution": meta_value("Attribution"),
            "media_type": "image",
            "provider": "Wikimedia Commons",
        })
    return results


def relevance_score(item: dict[str, Any], query: str, scene: dict[str, str],
                    intent_queries: list[str], identity: list[str]) -> tuple[int, list[str]]:
    title = str(item.get("title", "")).lower()
    creator = str(item.get("creator", "")).lower()
    haystack = f"{title} {creator}"
    local = set(words(" ".join(scene.get(k, "") for k in ("kicker", "headline"))))
    identity_set = set(identity)
    intent_terms = set(words(" ".join(intent_queries)))
    matched: list[str] = []
    score = 0

    for token in local:
        if token in haystack:
            score += 3
            matched.append(token)
    for token in identity_set:
        if token in haystack:
            score += 2
            matched.append(token)
    for token in intent_terms:
        if token in haystack:
            score += 1
            matched.append(token)

    # Exact phrase/query overlap gets a small bonus, but cannot alone make an
    # unrelated generic result pass.
    query_tokens = set(words(query))
    overlap = query_tokens.intersection(set(words(title)))
    if len(overlap) >= 2:
        score += 1
    return score, sorted(set(matched))


def search_scene(scene: dict[str, str], episode_title: str,
                 all_scenes: list[dict[str, str]], seen_sources: set[str]) -> tuple[str, str, list[dict[str, Any]], list[str]]:
    primary, queries, intent_name = make_queries(scene, episode_title, all_scenes)
    identity = extract_episode_identity(episode_title, all_scenes)
    _, intent_terms = infer_scene_intent(scene)

    collected: dict[str, dict[str, Any]] = {}
    for query in queries:
        try:
            found = commons_search(query)
        except Exception as exc:
            print(f"Search failed for '{query}': {exc}", file=sys.stderr)
            time.sleep(REQUEST_DELAY)
            continue

        for item in found:
            source = item.get("source_page", "")
            if not source or source in seen_sources:
                continue
            score, matched = relevance_score(item, query, scene, intent_terms, identity)
            item["relevance_score"] = score
            item["matched_terms"] = matched
            item["matched_query"] = query
            previous = collected.get(source)
            if previous is None or score > previous["relevance_score"]:
                collected[source] = item
        time.sleep(REQUEST_DELAY)

    ranked = sorted(
        collected.values(),
        key=lambda x: (x["relevance_score"], bool(x.get("license")), x.get("title", "")),
        reverse=True,
    )
    # Reject low relevance instead of filling the report with unrelated files.
    accepted = [x for x in ranked if x["relevance_score"] >= MIN_RELEVANCE_SCORE]
    return primary, intent_name, accepted[:MAX_CANDIDATES], queries


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
    if not scenes:
        print("No scenes found in episode script.", file=sys.stderr)
        return 2

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    report = [
        f"# Media review - {title}", "",
        "> Review required. Search results are candidates, not approved assets.",
        "> Low-relevance results are filtered. Check each original file page and license.", "",
        f"- Search mode: Scene-Aware Balanced",
        f"- Duplicate control: episode-wide source-page exclusion",
        f"- Relevance threshold: {MIN_RELEVANCE_SCORE}", "",
    ]
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "episode": episode_path.stem,
        "title": title,
        "search_mode": "scene_aware_balanced",
        "policy": {
            "only_approved_assets_render": True,
            "unknown_license_allowed": False,
            "ai_reconstructions_must_be_labelled": True,
            "automatic_approval": False,
        },
        "scenes": [],
    }

    seen_sources: set[str] = set()
    for scene in scenes:
        scene_no = scene.get("scene", str(len(manifest["scenes"]) + 1))
        primary_query, intent_name, candidates, queries = search_scene(
            scene, title, scenes, seen_sources
        )
        report.extend([
            f"## Scene {scene_no}",
            f"- Visual intent: `{intent_name}`",
            f"- Primary query: `{primary_query}`",
            f"- Search attempts: {len(queries)}",
        ])
        for attempt in queries:
            report.append(f"  - `{attempt}`")
        report.append("")

        scene_assets = []
        if not candidates:
            report.append(
                "- No sufficiently relevant new candidates found. "
                "Use a manual visual or AI reconstruction, or refine this scene's wording."
            )
        for idx, item in enumerate(candidates, start=1):
            asset_id = f"scene-{int(scene_no):02d}-candidate-{idx}" if scene_no.isdigit() else f"scene-{scene_no}-candidate-{idx}"
            source = item["source_page"]
            seen_sources.add(source)

            row = {
                "scene": scene_no,
                "asset_id": asset_id,
                "intent": intent_name,
                "query": item.get("matched_query", primary_query),
                "relevance_score": item["relevance_score"],
                "matched_terms": ", ".join(item["matched_terms"]),
                "title": item["title"],
                "provider": item["provider"],
                "media_type": item["media_type"],
                "preview_url": item["preview_url"],
                "download_url": item["download_url"],
                "source_page": source,
                "creator": item["creator"],
                "license": item["license"],
                "license_url": item["license_url"],
                "attribution": item["attribution"],
                "status": "pending",
            }
            rows.append(row)
            scene_assets.append({
                "asset_id": asset_id,
                "status": "pending",
                "media_type": item["media_type"],
                "kind": "historical_archive",
                "storage": "runtime",
                "download_url": item["download_url"],
                "source_page": source,
                "creator": item["creator"],
                "license": item["license"],
                "license_url": item["license_url"],
                "attribution": item["attribution"],
                "sha256": "",
                "relevance_score": item["relevance_score"],
                "matched_terms": item["matched_terms"],
                "notes": "Candidate only. Verify source page, relevance and license; do not render while pending.",
            })
            report.extend([
                f"### Candidate {idx}: {item['title']}",
                f"- Relevance score: {item['relevance_score']}",
                f"- Matched terms: {', '.join(item['matched_terms']) or 'None'}",
                f"- Source: {source}",
                f"- Creator: {item['creator'] or 'Not listed'}",
                f"- License: {item['license'] or 'Not listed - verify manually'}",
                f"- License details: {item['license_url'] or 'Not listed'}",
                f"- Preview: {item['preview_url']}",
                "- Status: **PENDING - NOT APPROVED**", "",
            ])

        manifest["scenes"].append({
            "scene": int(scene_no) if scene_no.isdigit() else scene_no,
            "visual_intent": intent_name,
            "search_query": primary_query,
            "search_attempts": queries,
            "assets": scene_assets,
        })

    with (out / "media-review.md").open("w", encoding="utf-8") as f:
        f.write("\n".join(report))
    csv_fields = [
        "scene", "asset_id", "intent", "query", "relevance_score",
        "matched_terms", "title", "provider", "media_type", "preview_url",
        "download_url", "source_page", "creator", "license", "license_url",
        "attribution", "status",
    ]
    with (out / "media-manifest.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(rows)
    (out / "media-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Review bundle created in: {out}")
    print(f"Scenes: {len(scenes)} | Relevant candidates: {len(rows)}")
    print("Audio and rendering pipeline were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
