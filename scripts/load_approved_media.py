#!/usr/bin/env python3
"""Load only manually approved, verified Wikimedia raster images for rendering."""
from __future__ import annotations
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

ALLOWED_HOST = "upload.wikimedia.org"
MAX_BYTES = 15 * 1024 * 1024
USER_AGENT = "MusicShortsFactoryRenderer/1.0 (approved archive media)"

def _download(url: str, destination: Path, expected_sha256: str = "") -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError("Only HTTPS files hosted on upload.wikimedia.org are allowed.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != ALLOWED_HOST:
            raise ValueError("Redirect left the approved Wikimedia upload host.")
        content_type = response.headers.get_content_type().lower()
        if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            raise ValueError(f"Unsupported image type: {content_type}")
        data = response.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Image is empty or larger than the 15 MB limit.")
    signatures = {
        "image/jpeg": data.startswith(bytes.fromhex("FFD8FF")),
        "image/png": data.startswith(bytes.fromhex("89504E470D0A1A0A")),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    if not signatures[content_type]:
        raise ValueError("Downloaded bytes do not match the declared image type.")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        raise ValueError("SHA-256 mismatch.")
    extension = {"image/jpeg": ".jpg", "image/png": ".png",
                 "image/gif": ".gif", "image/webp": ".webp"}[content_type]
    destination = destination.with_suffix(extension)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination.resolve().as_uri(), digest

def load_approved_media(manifest_path: Path, episode_stem: str,
                        work_dir: Path, scene_count: int) -> dict[int, str]:
    """Return scene number -> local file URI; unapproved assets safely fall back."""
    if not manifest_path.is_file():
        print(f"Media manifest not found ({manifest_path}); using original SVG visuals.")
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Cannot read media manifest ({exc}); using original SVG visuals.")
        return {}
    if manifest.get("episode") != episode_stem:
        print("WARNING: Manifest belongs to another episode; using original SVG visuals.")
        return {}
    loaded: dict[int, str] = {}
    for scene in manifest.get("scenes", []):
        try:
            number = int(scene.get("scene"))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= scene_count:
            continue
        for asset in scene.get("assets", []):
            if str(asset.get("status", "")).lower() != "approved":
                continue
            if str(asset.get("media_type", "")).lower() != "image":
                continue
            if not str(asset.get("license", "")).strip():
                print(f"Scene {number}: no license label; skipping asset.")
                continue
            url = str(asset.get("download_url", "")).strip()
            if not url:
                continue
            target = work_dir / "media_assets" / f"scene-{number:02d}"
            try:
                uri, digest = _download(url, target, str(asset.get("sha256", "")).strip())
                loaded[number] = uri
                print(f"Scene {number}: approved image loaded; SHA-256 {digest}.")
                break
            except Exception as exc:
                print(f"WARNING: Scene {number} asset skipped: {exc}")
    print(f"Approved archive images loaded: {len(loaded)} of {scene_count} scenes.")
    return loaded
