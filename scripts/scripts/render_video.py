#!/usr/bin/env python3
"""Draws every frame of the HTML animation in headless Chromium and pipes it into FFmpeg."""
import subprocess
import time
from pathlib import Path

MAX_RENDER_SECONDS = 20 * 60


def render(timeline, audio_path, out_path, template_path, log_path):
    from playwright.sync_api import sync_playwright

    fps = timeline["fps"]
    total_frames = int(round(timeline["total"] * fps))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg", "-i", "-",
        "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-t", f"{timeline['total']:.3f}",
        "-movflags", "+faststart",
        str(out_path),
    ]
    log = open(log_path, "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log)
    started = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--force-color-profile=srgb", "--font-render-hinting=none", "--disable-lcd-text"]
            )
            page = browser.new_page(viewport={"width": 1080, "height": 1920}, device_scale_factor=1)
            page.on("pageerror", lambda error: print(f"  PAGE ERROR: {error}", flush=True))
            page.goto(Path(template_path).resolve().as_uri())
            page.wait_for_function("typeof window.initTimeline === 'function'")
            page.evaluate("timeline => window.initTimeline(timeline)", timeline)
            page.evaluate("document.fonts.ready.then(() => true)")
            for index in range(total_frames):
                page.evaluate("t => window.renderFrame(t)", index / fps)
                if index == 0:
                    page.evaluate("document.fonts.ready.then(() => true)")
                frame = page.screenshot(type="jpeg", quality=95)
                try:
                    proc.stdin.write(frame)
                except (BrokenPipeError, OSError):
                    raise RuntimeError("FFmpeg stopped early. Open the render log in the output files.")
                if index % 90 == 0:
                    print(f"  frame {index}/{total_frames}", flush=True)
                if time.time() - started > MAX_RENDER_SECONDS:
                    raise RuntimeError("Rendering took longer than 20 minutes and was stopped.")
            browser.close()
    except Exception:
        proc.kill()
        log.close()
        raise
    proc.stdin.close()
    code = proc.wait()
    log.close()
    if code != 0:
        tail = Path(log_path).read_text(errors="ignore")[-800:]
        raise RuntimeError(f"FFmpeg failed (code {code}): {tail}")
