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
import os
from typing import Optional

from . import common

FIREWORKS_IMAGE_MODEL = os.environ.get(
    "FIREWORKS_IMAGE_MODEL", "accounts/fireworks/models/flux-1-schnell-fp8"
)
ARTIST_BACKEND, _api_key = common.resolve_backend(
    "artist",
    os.environ.get("ARTIST_BACKEND", "mock").strip().lower(),
    remote="fireworks",
    fallback="mock",
    missing_msg="FIREWORKS_API_KEY not set; falling back to mock (no images).",
)


def generate_image(prompt: str) -> Optional[str]:
    """
    Returns a data:image/... URI, or None if the backend is mock/off or
    the call failed. Never raises.
    """
    if ARTIST_BACKEND != "fireworks":
        return None

    import requests

    url = f"{common.FIREWORKS_BASE_URL}/workflows/{FIREWORKS_IMAGE_MODEL}/text_to_image"
    try:
        resp = requests.post(
            url,
            headers=common.auth_headers(_api_key),
            json={"prompt": prompt, "width": 1024, "height": 576, "steps": 4},
            timeout=45,
        )
        resp.raise_for_status()
        encoded = base64.b64encode(resp.content).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:  # noqa: BLE001 - image gen must never kill a scene
        print(f"[artist] Fireworks image call failed ({exc}); scene will have no image.")
        return None
