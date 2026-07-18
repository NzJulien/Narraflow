"""
Artist Agent
------------
Turns a scene's image prompt into a still image (base64 data URI) that
the Cinematographer agent then animates with Ken Burns motion (or hosted
video, once Fireworks ships one - see cinematographer.py).

NOTE: reconstructed here to match the exact interface orchestrator.py
and main.py already call (ARTIST_BACKEND, generate_image(prompt)) - it
wasn't part of the pasted bundle. Drop in your original module instead
if you have it; nothing else needs to change.

ARTIST_BACKEND: "mock" (no image - the frontend shows a text placeholder
and a Retry button) or "fireworks" (Fireworks-hosted FLUX image model,
served on AMD Instinct MI300X).

Same defensive contract as the rest of the pipeline: any failure here
returns None instead of raising, so a missing key or a flaky call never
takes a scene down - the frontend already handles a null image cleanly.
"""

import base64
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

ARTIST_BACKEND = os.environ.get("ARTIST_BACKEND", "mock").strip().lower()
FIREWORKS_IMAGE_MODEL = os.environ.get(
    "FIREWORKS_IMAGE_MODEL", "accounts/fireworks/models/flux-1-schnell-fp8"
)
FIREWORKS_BASE_URL = os.environ.get("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")
_api_key = os.environ.get("FIREWORKS_API_KEY", "").strip()

if ARTIST_BACKEND == "fireworks" and not _api_key:
    logger.warning("FIREWORKS_API_KEY not set; falling back to mock (no images).")
    ARTIST_BACKEND = "mock"


def generate_image(prompt: str) -> Optional[str]:
    """
    Returns a data:image/... URI, or None if the backend is mock/off or
    the call failed. Never raises.
    """
    if ARTIST_BACKEND != "fireworks":
        return None

    import requests

    url = f"{FIREWORKS_BASE_URL}/workflows/{FIREWORKS_IMAGE_MODEL}/text_to_image"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
            json={"prompt": prompt, "width": 1024, "height": 576, "steps": 4},
            timeout=45,
        )
        resp.raise_for_status()
        encoded = base64.b64encode(resp.content).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:  # noqa: BLE001 - image gen must never kill a scene
        logger.warning("Fireworks image call failed; scene will have no image.", exc_info=True)
        return None
