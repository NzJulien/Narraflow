"""HTTP surface for NarraFlow Studio (mounted under /api)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, demo, jobs, render, store, tools
from .models import Story

log = logging.getLogger("narraflow.studio.routes")
router = APIRouter(prefix="/api")

# no leading dot: rules out "." and ".." path segments outright
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _client_key(request: Request) -> str:
    if os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _story_or_404(story_id: str) -> Story:
    story = store.get(story_id)
    if story is None:
        raise HTTPException(404, "That story doesn't exist (it may have been cleared).")
    return story


@router.get("/health")
def health() -> Dict[str, Any]:
    from .images import get_provider

    try:
        provider = get_provider().name
    except Exception:  # noqa: BLE001
        provider = "unavailable"
    return {"ok": True, "voice_configured": agent.is_configured(), "image_provider": provider,
            "ffmpeg": bool(__import__("shutil").which("ffmpeg")), "jobs_in_flight": jobs.in_flight()}


# ---- stories -----------------------------------------------------------
@router.post("/stories")
def create_story(request: Request) -> Any:
    if not story_limiter.allow(_client_key(request)):
        return JSONResponse({"error": "You've started a lot of stories. Please wait a bit.", "code": "rate_limited"}, status_code=429)
    return store.save(Story()).model_dump()


@router.get("/stories/{story_id}")
def get_story(story_id: str) -> Dict[str, Any]:
    return _story_or_404(story_id).model_dump()


@router.get("/stories/{story_id}/events")
async def story_events(story_id: str, request: Request) -> StreamingResponse:
    """Server-sent events: a full story snapshot whenever it changes, so scene
    cards flip from 'painting' to the finished illustration on their own."""
    _story_or_404(story_id)

    async def stream():
        last = -1
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            story = await run_in_threadpool(store.get, story_id)
            if story is None:
                yield "event: gone\ndata: {}\n\n"
                return
            if story.version != last:
                last, idle = story.version, 0
                yield f"data: {json.dumps(story.model_dump())}\n\n"
            else:
                idle += 1
                if idle % 30 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- voice -------------------------------------------------------------
class VoiceSessionRequest(BaseModel):
    story_id: Optional[str] = None


@router.get("/voice/status")
def voice_status() -> Dict[str, Any]:
    return {"configured": agent.is_configured()}


@router.post("/voice/session")
def voice_session(req: VoiceSessionRequest, request: Request) -> Any:
    """Mint a single-use AssemblyAI token and return it with the session config.
    The permanent API key stays on the server."""
    if not agent.token_limiter.allow(_client_key(request)):
        return JSONResponse({"error": "You've started a lot of voice sessions. Please wait a bit and try again.",
                             "code": "rate_limited"}, status_code=429)
    story = store.get(req.story_id) if req.story_id else None
    try:
        token = agent.mint_token()
    except agent.VoiceError as exc:
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)
    return {"token": token, "ws_url": agent.ws_url(), "session": agent.build_session(story),
            "story_id": story.id if story else None}


# ---- tools -------------------------------------------------------------
class ToolRequest(BaseModel):
    story_id: Optional[str] = None
    arguments: Any = None


MAX_TOOL_BODY = 64 * 1024  # a spoken turn is a few KB; anything bigger is abuse
tool_limiter = agent.RateLimiter(int(os.environ.get("TOOL_CALLS_PER_HOUR", "600")), 3600.0)
story_limiter = agent.RateLimiter(int(os.environ.get("STORIES_PER_HOUR", "60")), 3600.0)


@router.post("/tools/{name}")
async def run_tool(name: str, request: Request) -> Any:
    raw = await request.body()
    if len(raw) > MAX_TOOL_BODY:
        return JSONResponse({"ok": False, "error": "That was too much to handle at once. Try a shorter passage."}, status_code=413)
    if not tool_limiter.allow(_client_key(request)):
        return JSONResponse({"ok": False, "error": "You're going a bit fast. Give it a minute and try again.",
                             "code": "rate_limited"}, status_code=429)
    try:
        req = ToolRequest.model_validate_json(raw or b"{}")
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "I couldn't read that request."}, status_code=400)
    return await run_in_threadpool(_run_tool, name, req)


def _run_tool(name: str, req: ToolRequest) -> Dict[str, Any]:
    story_id = req.story_id
    if not story_id or store.get(story_id) is None:
        story_id = store.save(Story()).id  # a spoken story with no prior click still works
    result = tools.execute(name, story_id, req.arguments)
    result.setdefault("story_id", story_id)
    return result


@router.get("/demo/script")
def demo_script() -> Any:
    return demo.script()


# ---- assets & export ---------------------------------------------------
@router.get("/assets/{story_id}/{name}")
def get_asset(story_id: str, name: str) -> FileResponse:
    if not (_SAFE_NAME.match(story_id) and _SAFE_NAME.match(name)):
        raise HTTPException(400, "bad asset name")
    root = os.path.realpath(os.path.join(store.data_dir(), "assets"))
    path = os.path.realpath(os.path.join(root, story_id, name))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):  # second guard: never leave assets/
        raise HTTPException(404, "asset not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.post("/stories/{story_id}/export")
def start_export(story_id: str) -> Dict[str, Any]:
    _story_or_404(story_id)
    return tools.execute("export_story", story_id, {})


@router.get("/stories/{story_id}/export.mp4")
def download_export(story_id: str) -> FileResponse:
    story = _story_or_404(story_id)
    path = os.path.join(render.exports_dir(), f"{story.id}.mp4")
    if not os.path.isfile(path):
        raise HTTPException(404, "This story hasn't been exported yet.")
    safe_title = re.sub(r"[^A-Za-z0-9 _-]", "", story.title).strip().replace(" ", "_") or "story"
    return FileResponse(path, media_type="video/mp4", filename=f"{safe_title}.mp4")


@router.get("/stories/{story_id}/captions.srt")
def download_captions(story_id: str) -> FileResponse:
    story = _story_or_404(story_id)
    path = os.path.join(render.exports_dir(), f"{story.id}.srt")
    if not os.path.isfile(path):
        raise HTTPException(404, "No captions yet. Export the story first.")
    return FileResponse(path, media_type="application/x-subrip", filename="captions.srt")
