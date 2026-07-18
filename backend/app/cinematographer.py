"""
Cinematographer Agent
----------------------
The "video" half of instant voice-to-video: takes each scene's still
image (already produced by the Artist agent) and decides how the camera
should move across it, so the frontend can animate it live the instant
it lands - a scene feels like it's playing, not just appearing.

Two swappable backends, picked by CINEMATOGRAPHER_BACKEND, mirroring the
exact same pattern writer.py uses for WRITER_BACKEND and artist.py
uses for ARTIST_BACKEND:

  kenburns (default)   Computes a small motion spec here - pan
                         direction, start/end zoom, duration - and the
                         browser animates it with a CSS transform over
                         the Artist agent's image. No extra model call,
                         no extra latency, always available. This is
                         what actually ships the "video" feel today.

  fireworks              Calls a hosted Fireworks image-to-video model,
                         if/when Fireworks serves one. As of this build
                         Fireworks has fast LLM and FLUX image inference
                         but no video generation model, so this path is
                         wired up and ready - flip CINEMATOGRAPHER_BACKEND
                         and set FIREWORKS_VIDEO_MODEL the day one ships,
                         no other code changes needed - but until then it
                         always falls back to kenburns per scene. This
                         keeps the whole voice-to-video pipeline
                         AMD/Fireworks-only for the hackathon instead of
                         reaching for a third-party video API to fill the
                         gap.

Same defensive contract as the rest of the pipeline: any failure here
degrades to the kenburns spec instead of raising, so a missing video
model or a flaky call never takes a scene down.

animate_scene() takes an optional camera_style hint (e.g. "handheld",
"sweeping", "static") so the frontend's camera-style picker has
something to actually plug into. It's optional and defaults to None, so
every existing call site keeps working unchanged. For kenburns it nudges
duration/zoom; for fireworks it's folded into the video prompt.
"""

import base64
import os
from typing import Optional

from . import common

FIREWORKS_VIDEO_MODEL = os.environ.get("FIREWORKS_VIDEO_MODEL", "").strip()

CINEMATOGRAPHER_BACKEND, _api_key = common.resolve_backend(
    "cinematographer",
    os.environ.get("CINEMATOGRAPHER_BACKEND", "kenburns").strip().lower(),
    remote="fireworks",
    fallback="kenburns",
    extra_ready=bool(FIREWORKS_VIDEO_MODEL),
    missing_msg=(
        "FIREWORKS_VIDEO_MODEL not set (Fireworks has no "
        "video model yet); falling back to kenburns."
    ),
    ready_msg=f"Fireworks video backend ready: {FIREWORKS_VIDEO_MODEL}",
)

# Varied per scene so a 5-scene story doesn't repeat the same pan twice.
PAN_DIRECTIONS = [
    "left-to-right", "right-to-left", "top-to-bottom", "bottom-to-top",
    "center-out", "center-in",
]

# Style multipliers applied on top of the pacing-driven base spec, so the
# camera-style picker has a visible effect regardless of backend.
CAMERA_STYLE_ADJUST = {
    "handheld": {"duration_mult": 0.85, "zoom_extra": 0.02},
    "sweeping": {"duration_mult": 1.35, "zoom_extra": 0.05},
    "static": {"duration_mult": 1.0, "zoom_extra": 0.0},
    "punch-in": {"duration_mult": 0.7, "zoom_extra": 0.08},
}


_log = common.make_logger("cinematographer")


def _kenburns_spec(scene_number: int, pacing: str, image_prompt: str, camera_style: Optional[str] = None) -> dict:
    """
    Deterministic-but-varied motion spec: same (scene, prompt) always
    gives the same pan, but different scenes/stories don't repeat.
    Pacing shapes the motion - climax pushes in tighter and faster,
    resolution eases back out slower. camera_style layers a further
    adjustment on top.
    """
    seed = common.seed_from(scene_number, image_prompt)
    direction = PAN_DIRECTIONS[seed % len(PAN_DIRECTIONS)]

    if pacing == "climax":
        zoom_start, zoom_end, duration_ms = 1.0, 1.18, 3200
    elif pacing == "resolution":
        zoom_start, zoom_end, duration_ms = 1.08, 1.0, 4500
    elif pacing == "twist":
        zoom_start, zoom_end, duration_ms = 1.0, 1.12, 2600
    else:
        zoom_start, zoom_end, duration_ms = 1.0, 1.08, 4000

    adjust = CAMERA_STYLE_ADJUST.get((camera_style or "").strip().lower())
    if adjust:
        duration_ms = round(duration_ms * adjust["duration_mult"])
        zoom_end = round(zoom_end + adjust["zoom_extra"], 3)

    return {
        "backend": "kenburns",
        "direction": direction,
        "zoom_start": zoom_start,
        "zoom_end": zoom_end,
        "duration_ms": duration_ms,
        "camera_style": camera_style or None,
    }


def _fireworks_video(image_data_uri: str, prompt: str, duration_ms: int) -> Optional[dict]:
    """
    Attempts a real Fireworks-hosted image-to-video call. Isolated on
    purpose: the day Fireworks ships a video model, only this function
    (and the env vars above) need to change - orchestrator.py and the
    frontend already speak the same {backend, ...} contract either way.
    """
    import requests

    url = f"{common.FIREWORKS_BASE_URL}/workflows/{FIREWORKS_VIDEO_MODEL}/image_to_video"
    try:
        response = requests.post(
            url,
            headers=common.auth_headers(_api_key),
            json={"image": image_data_uri, "prompt": prompt, "duration_ms": duration_ms},
            timeout=60,
        )
        response.raise_for_status()
        encoded = base64.b64encode(response.content).decode("ascii")
        return {"backend": "fireworks", "video": f"data:video/mp4;base64,{encoded}"}
    except Exception as exc:  # noqa: BLE001 - video gen must never kill a scene
        _log(f"Fireworks video call failed ({exc}); falling back to kenburns for this scene.")
        return None


def animate_scene(
    scene_number: int,
    pacing: str,
    image_prompt: str,
    image_data_uri: Optional[str],
    camera_style: Optional[str] = None,
) -> Optional[dict]:
    """
    Returns a motion spec describing how to turn a scene's still image
    into video, or None if there's no image to animate (Artist agent
    had no image for this scene - see artist.py's fallback behavior).
    Never raises.
    """
    if image_data_uri is None:
        return None

    prompt = f"{image_prompt}, {camera_style} camera style" if camera_style else image_prompt

    if CINEMATOGRAPHER_BACKEND == "fireworks":
        fallback = _kenburns_spec(scene_number, pacing, prompt, camera_style)
        result = _fireworks_video(image_data_uri, prompt, fallback["duration_ms"])
        return result or fallback

    return _kenburns_spec(scene_number, pacing, prompt, camera_style)
