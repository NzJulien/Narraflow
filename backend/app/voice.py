"""
Voice Agent
-----------
Turns spoken audio into a story idea, so "instant voice to video" can
start from a sentence spoken out loud instead of typed in.

Two paths, and the frontend picks between them automatically - this
file is only ever involved in one of them:

  1. BROWSER (preferred) - index.html uses the Web Speech API
     (`SpeechRecognition` / `webkitSpeechRecognition`) to transcribe
     locally in the browser: zero latency, no upload, no server cost.
     When it's available (Chrome, Edge, most Android browsers), this
     module is never called at all - the transcript goes straight into
     the idea box and triggers /story/stream like a normal typed idea.

  2. FIREWORKS (fallback) - when the browser has no SpeechRecognition
     support (Firefox, some iOS Safari versions) or the mic permission
     flow fails, the frontend records a short clip with MediaRecorder
     and POSTs it to /voice/transcribe, which calls this module. Uses
     Fireworks' hosted Whisper-v3 endpoint - the same AMD Instinct
     MI300X-served infrastructure as the Writer and Artist agents - so
     the whole voice-to-video path stays inside AMD/Fireworks with no
     third-party STT provider involved.

Set VOICE_BACKEND=fireworks + FIREWORKS_API_KEY to turn on the fallback:

    export VOICE_BACKEND=fireworks
    export FIREWORKS_API_KEY=...
    export FIREWORKS_AUDIO_MODEL=whisper-v3

Same defensive contract as artist.py / writer.py: any failure (missing
key, missing package, network hiccup, bad/empty audio) degrades to
returning None instead of raising. A flaky transcription should send the
person back to typing, never crash the request.
"""

import os
from typing import Optional

from . import common

FIREWORKS_AUDIO_MODEL = os.environ.get("FIREWORKS_AUDIO_MODEL", "whisper-v3")

VOICE_BACKEND, _api_key = common.resolve_backend(
    "voice",
    os.environ.get("VOICE_BACKEND", "mock").strip().lower(),
    remote="fireworks",
    fallback="mock",
    missing_msg="FIREWORKS_API_KEY not set; falling back to mock (no server-side transcription).",
    ready_msg=f"Fireworks Whisper backend ready: {FIREWORKS_AUDIO_MODEL}",
)

_log = common.make_logger("voice")


def transcribe(audio_bytes: bytes, filename: str = "clip.webm") -> Optional[str]:
    """
    Returns the transcribed text, or None if the fallback is disabled or
    the call failed. Never raises. This is only ever hit when the
    browser's own Web Speech API wasn't available on the client - see
    the module docstring.
    """
    if VOICE_BACKEND != "fireworks":
        return None

    if not audio_bytes:
        _log("received empty audio payload; nothing to transcribe.")
        return None

    import requests

    url = f"{common.FIREWORKS_BASE_URL}/audio/transcriptions"
    try:
        response = requests.post(
            url,
            headers=common.auth_headers(_api_key, content_type=False),
            files={"file": (filename, audio_bytes)},
            data={"model": FIREWORKS_AUDIO_MODEL, "response_format": "json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        text = (data.get("text") or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - transcription must never kill the request
        _log(f"transcription failed ({exc}); frontend should fall back to typing.")
        return None
