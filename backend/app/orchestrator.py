"""
Orchestrator
------------
Coordinates Director -> Writer -> Memory -> Artist -> Cinematographer
for a full story, and exposes a generator version
(`generate_story_stream`) that yields one event at a time so the API can
stream scenes, their prose tokens, their images, AND their motion specs
to the frontend as they're ready, instead of making the user wait for
the whole story.

CHANGES vs the original build (kept intentionally additive - the five
agents and their order are untouched):

  - Every agent call is timed with time.perf_counter() and reported via
    a "timing" event ({agent, scene, ms}), plus an "agent_start" event
    fired the instant a step begins. Together these are what let the
    frontend draw a real, truthful pipeline timeline and per-agent
    latency badges instead of a fake sleep()-based one.
  - A "memory_diff" event is yielded right after the Memory agent runs,
    so the frontend can show *what changed* in the world state at each
    scene, not just the final snapshot.
  - NEW: the streaming path now calls writer.write_scene_stream()
    instead of writing the whole scene in one shot, and yields a
    "scene_token" event per chunk as prose arrives - the frontend can
    render it "typing" live instead of waiting for the full paragraph.
    The final canonical "scene" event still fires afterwards unchanged,
    so nothing downstream (Memory/Artist/Cinematographer, or any
    consumer of the SSE contract) needs to know streaming happened.
  - Optional genre / tone / length / image_style / camera_style hints.
    Director and Writer signatures weren't available to extend safely
    for genre/tone/length, so those are folded into the idea text
    itself (a defensive, additive approach - see _apply_style_hints)
    rather than guessing at new keyword arguments those modules might
    not accept. image_style is appended to the image prompt before it
    reaches the Artist agent. camera_style is passed through to the
    Cinematographer agent, whose signature we do control.
  - _build_image_prompt() factors out the "append image_style to the
    prompt" line so generate_story() and generate_story_stream() can't
    drift out of sync.
  - The "done" event carries total_ms for the whole story.

Each step remains wrapped defensively: if the Writer backend hiccups on
a single scene, or the Artist or Cinematographer call fails, the story
keeps going instead of taking the whole demo down with it.
"""

import logging
import time
import uuid
from typing import Iterator, Optional

from . import artist, cinematographer
from .director import plan_story
from .memory import get_world, new_world, update_memory
from .writer import write_scene, write_scene_stream

logger = logging.getLogger(__name__)

FALLBACK_TEXT = (
    "The story pauses for a breath here - the Writer agent hit a "
    "snag, but the tale continues."
)


def _fallback_scene(context: dict) -> dict:
    i = context["scene_number"]
    pacing = context.get("pacing", "setup")
    return {
        "scene": i + 1,
        "title": f"Scene {i + 1}: {pacing.capitalize()}",
        "text": FALLBACK_TEXT,
        "image_prompt": f"{context.get('idea', '')}, scene {i + 1}, cinematic concept art",
        "characters_introduced": [],
        "locations_introduced": [],
        "items_gained": {},
        "items_lost": {},
        "fallback": True,
    }


def _safe_write_scene(context: dict) -> dict:
    try:
        return write_scene(context)
    except Exception:  # noqa: BLE001 - never let one scene kill the story
        logger.warning(
            "scene %s failed; using a fallback line.", context["scene_number"] + 1, exc_info=True
        )
        return _fallback_scene(context)


def _safe_write_scene_stream(context: dict) -> Iterator[tuple]:
    """Streaming counterpart to _safe_write_scene: yields ("token", str)
    chunks then ("done", scene_dict). Degrades to a single fallback
    token + fallback scene if the Writer backend raises mid-stream."""
    try:
        yield from write_scene_stream(context)
    except Exception:  # noqa: BLE001
        logger.warning(
            "scene %s streaming failed; using a fallback line.",
            context["scene_number"] + 1,
            exc_info=True,
        )
        fallback = _fallback_scene(context)
        yield "token", fallback["text"]
        yield "done", fallback


def _safe_generate_image(prompt: str):
    try:
        return artist.generate_image(prompt)
    except Exception:  # noqa: BLE001 - never let a bad image call kill the story
        logger.warning("image generation failed; scene will have no image.", exc_info=True)
        return None


def _safe_animate_scene(scene_number: int, pacing: str, image_prompt: str, image, camera_style: Optional[str] = None):
    try:
        return cinematographer.animate_scene(scene_number, pacing, image_prompt, image, camera_style=camera_style)
    except Exception:  # noqa: BLE001 - never let a bad motion call kill the story
        logger.warning("cinematography failed; scene will render as a still.", exc_info=True)
        return None


def _apply_style_hints(idea: str, genre: Optional[str], tone: Optional[str], length: Optional[str] = None) -> str:
    """
    Folds optional genre/tone/length hints into the idea text itself,
    since we don't control director.py / writer.py's signatures and
    don't want to guess at new keyword arguments they might not accept.
    A conservative, additive way to make the configurable UI controls
    actually influence generation.
    """
    hints = []
    if genre:
        hints.append(f"genre: {genre}")
    if tone:
        hints.append(f"tone: {tone}")
    if length:
        hints.append(f"length: {length}")
    if not hints:
        return idea
    return f"{idea} (requested {', '.join(hints)})"


def _build_image_prompt(scene: dict, image_style: Optional[str]) -> str:
    """
    Appends the optional image_style hint to a scene's Artist prompt.
    Pulled out into its own function because generate_story() and
    generate_story_stream() both need the exact same prompt (the
    Cinematographer needs to see the same text the Artist rendered).
    """
    base_prompt = scene["image_prompt"]
    if image_style:
        return f"{base_prompt}, {image_style} style"
    return base_prompt


def generate_story(
    idea: str,
    scene_count: int = 5,
    genre: Optional[str] = None,
    tone: Optional[str] = None,
    length: Optional[str] = None,
    image_style: Optional[str] = None,
    camera_style: Optional[str] = None,
) -> dict:
    """Non-streaming version: returns the full story at once, images and motion included."""
    story_id = str(uuid.uuid4())
    story_start = time.perf_counter()
    styled_idea = _apply_style_hints(idea, genre, tone, length)

    plan = plan_story(styled_idea, scene_count)
    new_world(story_id)

    scenes = []
    for i, pacing in enumerate(plan["arc"]):
        scene = _safe_write_scene({
            "idea": styled_idea,
            "scene_number": i,
            "pacing": pacing,
            "world": get_world(story_id),
            "length": length,
        })
        update_memory(story_id, scene)

        image_prompt = _build_image_prompt(scene, image_style)
        scene["image"] = _safe_generate_image(image_prompt)
        scene["motion"] = _safe_animate_scene(
            scene["scene"], pacing, image_prompt, scene["image"], camera_style=camera_style
        )
        scenes.append(scene)

    return {
        "story_id": story_id,
        "plan": plan,
        "scenes": scenes,
        "world": get_world(story_id),
        "total_ms": round((time.perf_counter() - story_start) * 1000),
    }


def generate_story_stream(
    idea: str,
    scene_count: int = 5,
    genre: Optional[str] = None,
    tone: Optional[str] = None,
    length: Optional[str] = None,
    image_style: Optional[str] = None,
    camera_style: Optional[str] = None,
) -> Iterator[dict]:
    """
    Streaming version: yields one JSON-able dict at a time.

    Event sequence per story:
      plan
      for each scene:
        agent_start(writer) -> scene_token* -> timing(writer) ->
        agent_start(memory) -> timing(memory) -> memory_diff -> scene ->
        agent_start(artist) -> timing(artist) -> image ->
        agent_start(cinematographer) -> timing(cinematographer) -> motion
      done (carries total_ms + final world state)

    Used by the SSE endpoint in main.py.
    """
    story_id = str(uuid.uuid4())
    story_start = time.perf_counter()
    styled_idea = _apply_style_hints(idea, genre, tone, length)

    t0 = time.perf_counter()
    plan = plan_story(styled_idea, scene_count)
    director_ms = round((time.perf_counter() - t0) * 1000)
    new_world(story_id)

    yield {"type": "timing", "story_id": story_id, "agent": "director", "scene": None, "ms": director_ms}
    yield {"type": "plan", "story_id": story_id, "plan": plan}

    for i, pacing in enumerate(plan["arc"]):
        yield {"type": "agent_start", "story_id": story_id, "agent": "writer", "scene": i + 1}
        t0 = time.perf_counter()
        scene = None
        for kind, payload in _safe_write_scene_stream({
            "idea": styled_idea,
            "scene_number": i,
            "pacing": pacing,
            "world": get_world(story_id),
            "length": length,
        }):
            if kind == "token":
                yield {"type": "scene_token", "story_id": story_id, "scene": i + 1, "token": payload}
            else:
                scene = payload
        if scene is None:
            # _safe_write_scene_stream always yields a final ("done", scene),
            # but guard so a contract violation degrades to a fallback scene
            # instead of raising a TypeError that would kill the whole stream.
            logger.warning("scene %s produced no final scene payload; using a fallback.", i + 1)
            scene = _fallback_scene({"idea": styled_idea, "scene_number": i, "pacing": pacing})
        yield {"type": "timing", "story_id": story_id, "agent": "writer", "scene": scene["scene"],
               "ms": round((time.perf_counter() - t0) * 1000)}

        yield {"type": "agent_start", "story_id": story_id, "agent": "memory", "scene": scene["scene"]}
        t0 = time.perf_counter()
        update_memory(story_id, scene)
        yield {"type": "timing", "story_id": story_id, "agent": "memory", "scene": scene["scene"],
               "ms": round((time.perf_counter() - t0) * 1000)}
        yield {
            "type": "memory_diff",
            "story_id": story_id,
            "scene": scene["scene"],
            "added_characters": scene.get("characters_introduced", []),
            "added_locations": scene.get("locations_introduced", []),
            "world": get_world(story_id),
        }

        yield {"type": "scene", "story_id": story_id, "scene": scene}

        image_prompt = _build_image_prompt(scene, image_style)
        yield {"type": "agent_start", "story_id": story_id, "agent": "artist", "scene": scene["scene"]}
        t0 = time.perf_counter()
        image = _safe_generate_image(image_prompt)
        scene["image"] = image
        yield {"type": "timing", "story_id": story_id, "agent": "artist", "scene": scene["scene"],
               "ms": round((time.perf_counter() - t0) * 1000)}
        yield {"type": "image", "story_id": story_id, "scene": scene["scene"], "image": image}

        yield {"type": "agent_start", "story_id": story_id, "agent": "cinematographer", "scene": scene["scene"]}
        t0 = time.perf_counter()
        motion = _safe_animate_scene(scene["scene"], pacing, image_prompt, image, camera_style=camera_style)
        scene["motion"] = motion
        yield {"type": "timing", "story_id": story_id, "agent": "cinematographer", "scene": scene["scene"],
               "ms": round((time.perf_counter() - t0) * 1000)}
        yield {"type": "motion", "story_id": story_id, "scene": scene["scene"], "motion": motion}

    yield {
        "type": "done",
        "story_id": story_id,
        "world": get_world(story_id),
        "total_ms": round((time.perf_counter() - story_start) * 1000),
    }
