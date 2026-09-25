"""
NarraFlow API
-------------
FastAPI app tying the five-agent pipeline (Director, Writer, Memory,
Artist, Cinematographer) plus the Voice agent to an HTTP surface: a
status endpoint the frontend badge polls, an SSE endpoint that streams a
story scene-by-scene (including per-token prose as the Writer agent
generates it), a fallback transcription endpoint for browsers without
Web Speech API support, and a scene-level image retry endpoint.

  - /story and /story/stream accept optional genre, tone, length,
    image_style, camera_style query params and forward them to the
    orchestrator (see orchestrator.py for how each is applied).
  - POST /scene/regenerate-image: lets the frontend retry a single
    scene's image without re-running the whole story, using the exact
    same artist.generate_image() call the pipeline already uses. This
    is what powers the "Retry image" button.
  - Status payload carries a static "gpu" field for the AMD badge in
    the UI (informational only - doesn't change backend behavior).
"""

import json
import os
import time
from threading import Lock
from typing import Dict, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import artist, cinematographer, narrator, storage, voice, writer
from .orchestrator import generate_story, generate_story_stream

app = FastAPI(title="NarraFlow")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    storage.init_db()


@app.get("/")
def status():
    """Polled by the status badge in index.html on page load."""
    return {
        "app": "NarraFlow",
        "writer_backend": writer.BACKEND_MODE,
        "model": writer.MODEL_NAME if writer.BACKEND_MODE == "vllm" else writer.FIREWORKS_MODEL,
        "artist_backend": artist.ARTIST_BACKEND,
        "voice_backend": voice.VOICE_BACKEND,
        "cinematographer_backend": cinematographer.CINEMATOGRAPHER_BACKEND,
        "gpu": "AMD Instinct MI300X (via Fireworks AI)",
    }


@app.get("/story")
def story(
    idea: str = Query(...),
    scenes: int = Query(5, ge=1, le=12),
    genre: Optional[str] = Query(None),
    tone: Optional[str] = Query(None),
    length: Optional[str] = Query(None),
    image_style: Optional[str] = Query(None),
    camera_style: Optional[str] = Query(None),
):
    """Non-streaming: full story, images, and motion specs in one response."""
    return generate_story(
        idea, scenes, genre=genre, tone=tone, length=length,
        image_style=image_style, camera_style=camera_style,
    )


@app.get("/story/stream")
def story_stream(
    idea: str = Query(...),
    scenes: int = Query(5, ge=1, le=12),
    genre: Optional[str] = Query(None),
    tone: Optional[str] = Query(None),
    length: Optional[str] = Query(None),
    image_style: Optional[str] = Query(None),
    camera_style: Optional[str] = Query(None),
):
    """
    SSE endpoint - yields `data: {...}\\n\\n` events as the pipeline
    produces them: timing(director) -> plan -> per scene (agent_start /
    scene_token* / timing / memory_diff / scene / image / motion
    events) -> done. Matches the parser in index.html's runLive().
    """
    def event_source():
        for event in generate_story_stream(
            idea, scenes, genre=genre, tone=tone, length=length,
            image_style=image_style, camera_style=camera_style,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@app.post("/voice/transcribe")
async def transcribe(file: UploadFile = File(...)) -> dict:
    """
    Server-side fallback for browsers without Web Speech API support
    (see voice.py's module docstring). The frontend only calls this when
    `SpeechRecognition` is unavailable client-side - most browsers never
    hit this endpoint at all.

    Returns {"text": "..."} on success, or {"text": null} if
    transcription is disabled or failed - never a 500, so a flaky/absent
    Fireworks backend just tells the user to type their idea instead.
    """
    audio_bytes = await file.read()
    text: Optional[str] = voice.transcribe(audio_bytes, filename=file.filename or "clip.webm")
    return {"text": text}


class RegenerateImageRequest(BaseModel):
    prompt: str


@app.post("/scene/regenerate-image")
def regenerate_image(req: RegenerateImageRequest) -> dict:
    """
    Retries the Artist agent for a single scene's prompt, without
    re-running Director/Writer/Memory for the whole story. Powers the
    per-scene "Retry image" button so one flaky Artist call doesn't
    force a full regenerate during a live demo.

    Never 500s - a failed retry just returns {"image": null} again.
    """
    try:
        image = artist.generate_image(req.prompt)
    except Exception:  # noqa: BLE001 - retry must never itself crash
        image = None
    return {"image": image}


# ---------------------------------------------------------------------------
# Narration - real-time personal-story recording.
#
# Unlike the fiction pipeline above (one request, one SSE stream, nothing
# persisted), a narration session is built incrementally out of many small
# requests as the user speaks, and its whole point is that it survives a
# restart - so every write here goes straight through storage.py to SQLite.
#
# The only in-memory state is the small "unprocessed transcript" buffer per
# in-progress session, used purely to decide *when* to trigger the next
# story-update call (see _maybe_update_story). The durable transcript itself
# is appended to the database before that decision is even made, so a crash
# or a flaky Narrator call can never lose words the user already spoke.
# ---------------------------------------------------------------------------

_buffer_lock = Lock()
# story_id -> {"text": str, "last_update": float}
_pending: Dict[str, dict] = {}

SEGMENT_WORD_TRIGGER = 12
SENTENCE_WORD_TRIGGER = 4
TIME_TRIGGER_SECONDS = 6.0


def _public(story: dict) -> dict:
    """Replaces the server-local audio_path with a boolean before a story record
    leaves the API - the frontend only needs to know whether a player has something
    to point at, not where the file lives on disk."""
    story = dict(story)
    story["has_audio"] = bool(story.get("audio_path"))
    story.pop("audio_path", None)
    return story


class StartNarrationRequest(BaseModel):
    language: str = "en"


@app.post("/narration/start")
def start_narration(req: StartNarrationRequest) -> dict:
    story = storage.create_story(language=req.language or "en")
    with _buffer_lock:
        _pending[story["id"]] = {"text": "", "last_update": time.monotonic()}
    return {"id": story["id"], "created_at": story["created_at"]}


class NarrationSegmentRequest(BaseModel):
    id: str
    text: str


def _should_update(buffer_text: str, last_update: float) -> bool:
    if not buffer_text.strip():
        return False
    words = buffer_text.split()
    if len(words) >= SEGMENT_WORD_TRIGGER:
        return True
    if buffer_text.rstrip()[-1:] in ".!?" and len(words) >= SENTENCE_WORD_TRIGGER:
        return True
    if time.monotonic() - last_update >= TIME_TRIGGER_SECONDS:
        return True
    return False


@app.post("/narration/segment")
def narration_segment(req: NarrationSegmentRequest) -> dict:
    story = storage.get_story(req.id)
    if story is None:
        raise HTTPException(404, "narration session not found")

    text = (req.text or "").strip()
    if text:
        story = storage.append_transcript(req.id, text)

    with _buffer_lock:
        state = _pending.setdefault(req.id, {"text": "", "last_update": time.monotonic()})
        if text:
            state["text"] = f"{state['text']} {text}".strip()
        buffer_text = state["text"]
        last_update = state["last_update"]

    story_updated = False
    if _should_update(buffer_text, last_update):
        try:
            new_story_text = narrator.update_story(story["story_text"], buffer_text, story["language"])
        except Exception as exc:  # noqa: BLE001 - a flaky update must never drop the buffer
            print(f"[main] narration story update failed ({exc}); will retry on next segment.")
        else:
            story = storage.update_story(req.id, story_text=new_story_text)
            story_updated = True
            with _buffer_lock:
                _pending[req.id] = {"text": "", "last_update": time.monotonic()}

    return {"transcript": story["transcript"], "story": story["story_text"], "story_updated": story_updated}


class StopNarrationRequest(BaseModel):
    id: str
    duration_seconds: Optional[float] = None


@app.post("/narration/stop")
def stop_narration(req: StopNarrationRequest) -> dict:
    story = storage.get_story(req.id)
    if story is None:
        raise HTTPException(404, "narration session not found")

    with _buffer_lock:
        state = _pending.pop(req.id, None)
    remaining = (state or {}).get("text", "").strip()
    if remaining:
        try:
            new_story_text = narrator.update_story(story["story_text"], remaining, story["language"])
            story = storage.update_story(req.id, story_text=new_story_text)
        except Exception as exc:  # noqa: BLE001
            print(f"[main] final narration flush failed ({exc}); story keeps its last saved state.")

    title = story["title"]
    if not title:
        try:
            title = narrator.generate_title(story["story_text"], story["transcript"])
        except Exception as exc:  # noqa: BLE001
            print(f"[main] title generation failed ({exc}); using fallback.")
            title = "Untitled Story"

    story = storage.update_story(
        req.id, title=title, status="completed", duration_seconds=req.duration_seconds
    )
    return _public(story)


@app.post("/narration/audio")
async def upload_narration_audio(id: str, file: UploadFile = File(...)) -> dict:
    story = storage.get_story(id)
    if story is None:
        raise HTTPException(404, "narration session not found")
    path = os.path.join(storage.AUDIO_DIR, f"{id}.webm")
    with open(path, "wb") as f:
        f.write(await file.read())
    storage.update_story(id, audio_path=path)
    return {"ok": True}


@app.get("/narration")
def list_narrations() -> list:
    return storage.list_stories()


@app.get("/narration/{story_id}")
def get_narration(story_id: str) -> dict:
    story = storage.get_story(story_id)
    if story is None:
        raise HTTPException(404, "narration session not found")
    return _public(story)


class UpdateNarrationRequest(BaseModel):
    title: Optional[str] = None
    story_text: Optional[str] = None


@app.patch("/narration/{story_id}")
def update_narration(story_id: str, req: UpdateNarrationRequest) -> dict:
    story = storage.get_story(story_id)
    if story is None:
        raise HTTPException(404, "narration session not found")
    story = storage.update_story(story_id, title=req.title, story_text=req.story_text)
    return _public(story)


@app.get("/narration/{story_id}/audio")
def get_narration_audio(story_id: str):
    story = storage.get_story(story_id)
    if story is None or not story.get("audio_path") or not os.path.exists(story["audio_path"]):
        raise HTTPException(404, "no audio for this narration")
    return FileResponse(story["audio_path"], media_type="audio/webm")


# Serves the demo UI at /app/ - matches the Dockerfile's documented
# `http://localhost:8000/app/` entry point.
app.mount("/app", StaticFiles(directory="../frontend", html=True), name="frontend")
