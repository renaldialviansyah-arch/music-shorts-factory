
#!/usr/bin/env python3
"""Load only manually approved archive or AI reconstruction images."""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

ALLOWED_HOST = "upload.wikimedia.org"
MAX_BYTES = 15 * 1024 * 1024
USER_AGENT = "MusicShortsFactoryRenderer/1.1 (approved media)"


def _verify_image(data: bytes, content_type: str) -> str:
    signatures = {
        "image/jpeg": (
            data.startswith(bytes.fromhex("FFD8FF")), ".jpg"
        ),
        "image/png": (
            data.startswith(bytes.fromhex("89504E470D0A1A0A")), ".png"
        ),
        "image/gif": (
            data.startswith((b"GIF87a", b"GIF89a")), ".gif"
        ),
        "image/webp": (
            len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP",
            ".webp"
        ),
    }

    valid, extension = signatures.get(
        content_type, (False, "")
    )

    if not valid:
        raise ValueError(
            "Image bytes do not match a supported image type."
        )

    return extension


def _download_archive(
    url: str,
    destination: Path,
    expected_sha256: str = ""
) -> tuple[str, str]:

    parsed = urllib.parse.urlparse(url)

    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_HOST
    ):
        raise ValueError(
            "Only HTTPS files hosted on upload.wikimedia.org are allowed."
        )

    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        final = urllib.parse.urlparse(response.geturl())

        if (
            final.scheme != "https"
            or final.hostname != ALLOWED_HOST
        ):
            raise ValueError(
                "Redirect left the approved Wikimedia upload host."
            )

        content_type = (
            response.headers.get_content_type().lower()
        )

        data = response.read(MAX_BYTES + 1)

    if not data or len(data) > MAX_BYTES:
        raise ValueError(
            "Image is empty or larger than the 15 MB limit."
        )

    extension = _verify_image(data, content_type)
    digest = hashlib.sha256(data).hexdigest()

    if (
        expected_sha256
        and digest.lower() != expected_sha256.lower()
    ):
        raise ValueError("SHA-256 mismatch.")

    destination = destination.with_suffix(extension)
    destination.parent.mkdir(
        parents=True,
        exist_ok=True
    )
    destination.write_bytes(data)

    return destination.resolve().as_uri(), digest


def _load_local_ai(
    asset: dict,
    repo_root: Path
) -> tuple[str, str]:

    relative = str(
        asset.get("local_path", "")
    ).strip()

    if not relative:
        raise ValueError("AI image has no local_path.")

    candidate = (
        repo_root / relative
    ).resolve()

    allowed_root = (
        repo_root / "media" / "ai-reconstructions"
    ).resolve()

    if allowed_root not in candidate.parents:
        raise ValueError(
            "AI image path is outside media/ai-reconstructions."
        )

    if not candidate.is_file():
        raise FileNotFoundError(
            f"AI image file not found: {relative}"
        )

    if candidate.suffix.lower() not in {
        ".png", ".jpg", ".jpeg"
    }:
        raise ValueError("AI image must be PNG or JPEG.")

    data = candidate.read_bytes()

    if not data or len(data) > MAX_BYTES:
        raise ValueError(
            "AI image is empty or larger than the 15 MB limit."
        )

    mime = (
        "image/png"
        if candidate.suffix.lower() == ".png"
        else "image/jpeg"
    )

    _verify_image(data, mime)

    digest = hashlib.sha256(data).hexdigest()

    expected = str(
        asset.get("sha256", "")
    ).strip()

    if (
        expected
        and digest.lower() != expected.lower()
    ):
        raise ValueError("AI image SHA-256 mismatch.")

    return candidate.as_uri(), digest


def load_approved_media(
    manifest_path: Path,
    episode_stem: str,
    work_dir: Path,
    scene_count: int
) -> dict[int, dict[str, str]]:
    """Return scene -> {url,label}; only manually approved assets."""

    if not manifest_path.is_file():
        print(
            f"Media manifest not found ({manifest_path}); "
            "using original SVG visuals."
        )
        return {}

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"WARNING: Cannot read media manifest ({exc}); "
            "using original SVG visuals."
        )
        return {}

    if manifest.get("episode") != episode_stem:
        print(
            "WARNING: Manifest belongs to another episode; "
            "using original SVG visuals."
        )
        return {}

    repo_root = work_dir.resolve().parent
    loaded: dict[int, dict[str, str]] = {}

    for scene in manifest.get("scenes", []):
        try:
            number = int(scene.get("scene"))
        except (TypeError, ValueError):
            continue

        if not 1 <= number <= scene_count:
            continue

        for asset in scene.get("assets", []):

            if (
                str(asset.get("status", "")).lower()
                != "approved"
            ):
                continue

            if (
                str(asset.get("media_type", "")).lower()
                != "image"
            ):
                continue

            kind = str(
                asset.get(
                    "kind",
                    asset.get("source_type", "")
                )
            ).lower()

            is_ai = (
                kind == "ai_reconstruction"
                or str(
                    asset.get("source_type", "")
                ).lower() == "ai_reconstruction"
            )

            if is_ai:
                try:
                    uri, digest = _load_local_ai(
                        asset, repo_root
                    )

                    loaded[number] = {
                        "url": uri,
                        "label": "AI RECONSTRUCTION"
                    }

                    print(
                        f"Scene {number}: approved AI "
                        f"reconstruction loaded; SHA-256 {digest}."
                    )
                    break

                except Exception as exc:
                    print(
                        f"WARNING: Scene {number} AI asset "
                        f"skipped: {exc}"
                    )
                    continue

            if not str(asset.get("license", "")).strip():
                print(
                    f"Scene {number}: no license label; "
                    "skipping archive asset."
                )
                continue

            url = str(
                asset.get("download_url", "")
            ).strip()

            if not url:
                continue

            target = (
                work_dir
                / "media_assets"
                / f"scene-{number:02d}"
            )

            try:
                uri, digest = _download_archive(
                    url,
                    target,
                    str(asset.get("sha256", "")).strip()
                )

                loaded[number] = {
                    "url": uri,
                    "label": "HISTORICAL ARCHIVE"
                }

                print(
                    f"Scene {number}: approved archive "
                    f"image loaded; SHA-256 {digest}."
                )
                break

            except Exception as exc:
                print(
                    f"WARNING: Scene {number} archive "
                    f"asset skipped: {exc}"
                )

    print(
        f"Approved media images loaded: "
        f"{len(loaded)} of {scene_count} scenes."
    )

    return loaded
