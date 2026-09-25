"""Background generation.

Image generation and video rendering never block the voice conversation: a tool
call marks the work `generating`, returns immediately, and a worker thread does
the slow part. The UI learns about completion through the story event stream.

Guarantees:
  * a scene is only regenerated when its composed prompt actually changed
    (or when `force` is set) - so 'Show me the whole story' costs nothing;
  * a stale result (the scene was edited again while it rendered) is dropped;
  * a provider failure marks the scene `failed` with a human-readable reason
    and leaves the rest of the story intact.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import store
from .images import ImageError, SceneRequest, get_provider
from .models import Character, Scene, Story
from .prompts import compose_portrait_prompt, compose_scene_prompt, prompt_hash

log = logging.getLogger("narraflow.studio.jobs")

_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("IMAGE_WORKERS", "3")), thread_name_prefix="nf-job")

ASSET_URL = "/api/assets/{story_id}/{name}"

_inflight = 0
_inflight_lock = threading.Lock()


def _submit(fn, *args) -> None:
    """Submit to the pool while counting in-flight jobs (used by wait_idle and health)."""
    global _inflight
    with _inflight_lock:
        _inflight += 1

    def _done(_f) -> None:
        global _inflight
        with _inflight_lock:
            _inflight -= 1

    _pool.submit(fn, *args).add_done_callback(_done)


def in_flight() -> int:
    return _inflight


def assets_dir(story_id: str) -> str:
    path = os.path.join(store.data_dir(), "assets", story_id)
    os.makedirs(path, exist_ok=True)
    return path


def _seed(story: Story, order: int = 0) -> int:
    return int(re.sub(r"\D", "", story.id) or "7") % 1_000_003 + order * 13


def _scene_request(story: Story, scene: Scene) -> SceneRequest:
    chars = [c for c in (story.character(n) for n in scene.characters) if c]
    loc = story.location(scene.setting) if scene.setting else None
    text = " ".join([
        scene.setting, loc.description if loc else "", " ".join(loc.visual_traits) if loc else "",
        scene.summary, scene.visual_prompt, scene.revision_note, scene.emotion, story.tone,
        story.visual_style, " ".join(scene.actions),
    ]).lower()
    return SceneRequest(prompt=compose_scene_prompt(story, scene), seed=_seed(story), text=text, characters=chars)


def enqueue_scene(story_id: str, scene_id: str, *, force: bool = False) -> bool:
    """Queue image generation for one scene. Returns True if work was queued."""
    story = store.get(story_id)
    scene = next((s for s in story.scenes if s.id == scene_id), None) if story else None
    if not story or not scene:
        return False
    want = prompt_hash(_scene_request(story, scene).prompt + f"|r{scene.revision}")
    if not force and scene.status == "ready" and scene.prompt_hash == want:
        log.info("image.skip story=%s scene=%s reason=cached", story_id, scene_id)
        return False

    def mark(s: Story) -> None:
        sc = next(x for x in s.scenes if x.id == scene_id)
        sc.status, sc.error = "generating", ""

    store.update(story_id, mark)
    _submit(_run_scene, story_id, scene_id, want)
    return True


def _run_scene(story_id: str, scene_id: str, want_hash: str) -> None:
    log.info("image.start story=%s scene=%s", story_id, scene_id)
    story = store.get(story_id)
    scene = next((s for s in story.scenes if s.id == scene_id), None) if story else None
    if not story or not scene:
        return
    req = _scene_request(story, scene)
    revision = scene.revision
    try:
        provider = get_provider()
        png = provider.regenerate_scene(req) if revision else provider.generate_scene(req)
        name = f"scene-{scene_id}-r{revision}-{want_hash}.png"
        with open(os.path.join(assets_dir(story_id), name), "wb") as f:
            f.write(png)
    except ImageError as exc:
        _fail(story_id, scene_id, str(exc))
        return
    except Exception:  # noqa: BLE001 - a worker must never die silently
        log.exception("image.error story=%s scene=%s", story_id, scene_id)
        _fail(story_id, scene_id, "Something went wrong while painting this scene. You can ask me to try again.")
        return

    def done(s: Story) -> None:
        sc = next((x for x in s.scenes if x.id == scene_id), None)
        if sc is None:
            return  # scene was removed while rendering
        current = prompt_hash(_scene_request(s, sc).prompt + f"|r{sc.revision}")
        if current != want_hash:
            log.info("image.stale story=%s scene=%s", story_id, scene_id)
            return  # edited again mid-render; the newer job owns the result
        sc.image_url = ASSET_URL.format(story_id=story_id, name=name)
        sc.prompt_hash = want_hash
        sc.status, sc.error = "ready", ""

    store.update(story_id, done)
    log.info("image.done story=%s scene=%s", story_id, scene_id)


def _fail(story_id: str, scene_id: str, message: str) -> None:
    def mark(s: Story) -> None:
        sc = next((x for x in s.scenes if x.id == scene_id), None)
        if sc:
            sc.status, sc.error = "failed", message

    store.update(story_id, mark)
    log.warning("image.failed story=%s scene=%s msg=%s", story_id, scene_id, message)


def enqueue_portrait(story_id: str, character_id: str) -> None:
    _submit(_run_portrait, story_id, character_id)


def _run_portrait(story_id: str, character_id: str) -> None:
    story = store.get(story_id)
    char: Optional[Character] = next((c for c in story.characters if c.id == character_id), None) if story else None
    if not story or not char:
        return
    try:
        req = SceneRequest(prompt=compose_portrait_prompt(story, char), seed=_seed(story),
                           text=" ".join([char.description, story.visual_style]).lower(), characters=[char])
        png = get_provider().generate_character_reference(req)
        name = f"char-{character_id}-{prompt_hash(req.prompt)}.png"
        with open(os.path.join(assets_dir(story_id), name), "wb") as f:
            f.write(png)
    except Exception:  # noqa: BLE001 - a missing portrait is cosmetic
        log.warning("portrait.failed story=%s char=%s", story_id, character_id)
        return

    def done(s: Story) -> None:
        c = next((x for x in s.characters if x.id == character_id), None)
        if c:
            c.portrait_url = ASSET_URL.format(story_id=story_id, name=name)

    store.update(story_id, done)


def wait_idle(timeout: float = 30.0) -> bool:
    """Block until every queued/running job has finished (used by tests). True if idle."""
    import time

    end = time.time() + timeout
    while time.time() < end:
        if _inflight == 0:
            return True
        time.sleep(0.05)
    return False
