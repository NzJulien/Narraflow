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
     Fireworks' hosted Whisper-v3 endpoint, the same provider as the
     Writer and Artist agents. (The new voice studio does not use this
     module: it uses the AssemblyAI Voice Agent API.)

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

VOICE_BACKEND = os.environ.get("VOICE_BACKEND", "mock").strip().lower()
FIREWORKS_AUDIO_MODEL = os.environ.get("FIREWORKS_AUDIO_MODEL", "whisper-v3")
FIREWORKS_BASE_URL = os.environ.get(
    "FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1"
)

_api_key: Optional[str] = None

if VOICE_BACKEND == "fireworks":
    _api_key = os.environ.get("FIREWORKS_API_KEY", "").strip()
    if not _api_key:
        print("[voice] FIREWORKS_API_KEY not set; falling back to mock (no server-side transcription).")
        VOICE_BACKEND = "mock"
    else:
        print(f"[voice] Fireworks Whisper backend ready: {FIREWORKS_AUDIO_MODEL}")


def _log(msg: str) -> None:
    print(f"[voice] {msg}")


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

    url = f"{FIREWORKS_BASE_URL}/audio/transcriptions"
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {_api_key}"},
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
