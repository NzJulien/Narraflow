"""Illustrated story export.

Turns the scene illustrations into a captioned MP4 (ffmpeg): a title card, then
each scene with a slow Ken Burns pan/zoom, fades between scenes and the scene's
narration burned in as a caption. An .srt file is written alongside.

The exported video is silent: NarraFlow has no server-side text-to-speech.
Narration is spoken during in-app playback (browser speech synthesis).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from . import jobs, store
from .models import Scene, Story

log = logging.getLogger("narraflow.studio.render")
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nf-render")
W, H, FPS = 1280, 720, 30
TITLE_SECONDS = 3.0
FADE = 0.5

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/dejavu/DejaVuSerif.ttf",
    "/Library/Fonts/Georgia.ttf",
]


class RenderError(RuntimeError):
    pass


def exports_dir() -> str:
    path = os.path.join(store.data_dir(), "exports")
    os.makedirs(path, exist_ok=True)
    return path


def _font(size: int) -> ImageFont.ImageFont:
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> List[str]:
    lines: List[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return [ln for ln in lines if ln] or [""]


def caption_frame(image_path: str, caption: str) -> Image.Image:
    """Illustration with a soft caption bar (the words the storyteller spoke)."""
    img = Image.open(image_path).convert("RGB").resize((W, H), Image.LANCZOS)
    if not caption.strip():
        return img
    font = _font(30)
    probe = ImageDraw.Draw(img)
    lines = _wrap(probe, caption.strip(), font, W - 160)[:4]
    bar_h = 30 + len(lines) * 42
    bar = Image.new("RGBA", (W, bar_h), (18, 14, 26, 190))
    img = img.convert("RGBA")
    img.alpha_composite(bar, (0, H - bar_h))
    d = ImageDraw.Draw(img)
    y = H - bar_h + 18
    for line in lines:
        d.text((80, y), line, font=font, fill=(255, 250, 240))
        y += 42
    return img.convert("RGB")


def title_frame(title: str, subtitle: str = "An illustrated story told by voice") -> Image.Image:
    img = Image.new("RGB", (W, H), (28, 22, 44))
    d = ImageDraw.Draw(img)
    big, small = _font(64), _font(28)
    lines = _wrap(d, title, big, W - 240)[:3]
    y = H // 2 - len(lines) * 40 - 20
    for line in lines:
        d.text(((W - d.textlength(line, font=big)) / 2, y), line, font=big, fill=(255, 240, 210))
        y += 80
    d.text(((W - d.textlength(subtitle, font=small)) / 2, y + 20), subtitle, font=small, fill=(190, 180, 220))
    return img


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def build_srt(story: Story, scenes: List[Scene]) -> str:
    t, out = TITLE_SECONDS, []
    for i, sc in enumerate(scenes, start=1):
        out.append(f"{i}\n{_srt_time(t)} --> {_srt_time(t + sc.duration)}\n{(sc.narration or sc.summary).strip()}\n")
        t += sc.duration
    return "\n".join(out)


def _run(cmd: List[str]) -> None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise RenderError("Video rendering needs ffmpeg, which is not installed on this server.") from None
    except subprocess.TimeoutExpired:
        raise RenderError("Rendering took too long and was stopped. Try a shorter story.") from None
    if r.returncode != 0:
        log.error("ffmpeg failed: %s", r.stderr[-600:])
        raise RenderError("The video renderer hit an error. Try exporting again.")


def _segment(png: str, seconds: float, out: str, idx: int, fade_in: bool = True) -> None:
    frames = max(1, int(seconds * FPS))
    zoom_in = idx % 2 == 0
    z = "min(zoom+0.0008,1.12)" if zoom_in else "if(eq(on,0),1.12,max(zoom-0.0008,1.0))"
    vf = (f"scale={W * 2}:{H * 2},zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
          f"fade=t=in:st=0:d={FADE},fade=t=out:st={max(0.0, seconds - FADE):.2f}:d={FADE},format=yuv420p")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", png, "-vf", vf, "-t", f"{seconds:.2f}",
          "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", out])


def render_story(story: Story) -> Tuple[str, str]:
    """Blocking render. Returns (mp4_path, srt_path)."""
    scenes = [s for s in story.scenes if s.status == "ready" and s.image_url]
    if not scenes:
        raise RenderError("No scene is illustrated yet, so there is nothing to render.")
    base = jobs.assets_dir(story.id)
    with tempfile.TemporaryDirectory(prefix="nf-render-") as tmp:
        segs: List[str] = []
        t_png = os.path.join(tmp, "title.png")
        title_frame(story.title).save(t_png)
        seg = os.path.join(tmp, "seg000.mp4")
        _segment(t_png, TITLE_SECONDS, seg, 1)
        segs.append(seg)
        for i, sc in enumerate(scenes):
            src = os.path.join(base, os.path.basename(sc.image_url))
            if not os.path.exists(src):
                raise RenderError(f"The illustration for scene {sc.order} is missing. Regenerate it and try again.")
            png = os.path.join(tmp, f"s{i:03d}.png")
            caption_frame(src, sc.narration or sc.summary).save(png)
            seg = os.path.join(tmp, f"seg{i + 1:03d}.mp4")
            _segment(png, sc.duration, seg, i)
            segs.append(seg)
        listing = os.path.join(tmp, "list.txt")
        with open(listing, "w") as f:
            f.write("".join(f"file '{p}'\n" for p in segs))
        out_mp4 = os.path.join(exports_dir(), f"{story.id}.mp4")
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing,
              "-c", "copy", "-movflags", "+faststart", out_mp4])
    out_srt = os.path.join(exports_dir(), f"{story.id}.srt")
    with open(out_srt, "w", encoding="utf-8") as f:
        f.write(build_srt(story, scenes))
    return out_mp4, out_srt


def enqueue_export(story_id: str) -> None:
    def mark(s: Story) -> None:
        s.status, s.export_error, s.export_url = "rendering", "", None

    store.update(story_id, mark)
    jobs._submit(_export_job, story_id)  # counted in-flight like image jobs


def _export_job(story_id: str) -> None:
    log.info("render.start story=%s", story_id)
    story = store.get(story_id)
    if not story:
        return
    try:
        mp4, _ = render_story(story)
    except RenderError as exc:
        _finish(story_id, None, str(exc))
        return
    except Exception:  # noqa: BLE001
        log.exception("render.error story=%s", story_id)
        _finish(story_id, None, "The video export failed unexpectedly. Try again.")
        return
    _finish(story_id, f"/api/stories/{story_id}/export.mp4", "")
    log.info("render.done story=%s file=%s", story_id, os.path.basename(mp4))


def _finish(story_id: str, url: Optional[str], error: str) -> None:
    def mark(s: Story) -> None:
        s.status = "complete" if url else "draft"
        s.export_url, s.export_error = url, error

    store.update(story_id, mark)
