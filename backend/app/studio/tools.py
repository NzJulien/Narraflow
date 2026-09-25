"""The voice agent's tools.

Each tool has (a) a JSON-schema definition in the format AssemblyAI's Voice
Agent API expects (`{"type": "function", "name", "description", "parameters"}`)
and (b) an executor that mutates the structured story.

The agent's own LLM does the narrative understanding: it turns spoken narration
into structured arguments (characters, locations, scenes, visual descriptions).
The executors validate, merge and persist that structure, keep character and
location traits persistent, and queue image jobs. Nothing here blocks on image
generation, so a tool call returns in milliseconds and the agent stays
conversational.

Tool arguments come from an LLM, so every executor is lenient about types
(numbers as strings, a single string where a list was expected) and strict
about meaning (unknown scene numbers and unconfirmed deletions are refused
with a message the agent can speak).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

from . import jobs, store
from .models import Character, Location, Scene, Story
from .prompts import derive_traits

log = logging.getLogger("narraflow.studio.tools")


MAX_SCENES = int(os.environ.get("MAX_SCENES_PER_STORY", "16"))  # each scene costs an image generation


class ToolError(Exception):
    """A refusal the agent can relay to the user in plain words."""


# ---------------------------------------------------------------------------
# lenient coercion helpers (LLM arguments are untrusted)
# ---------------------------------------------------------------------------
def _s(v: Any, default: str = "") -> str:
    if v is None:
        return default
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip()


def _list(v: Any) -> List[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [p.strip() for p in re.split(r"[;\n]|,(?![^()]*\))", v) if p.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def _int(v: Any, name: str) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        raise ToolError(f"I need a scene number for {name}.") from None


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "confirmed")
    return bool(v)


def _need_scene(story: Story, number: Any) -> Scene:
    n = _int(number, "that")
    scene = story.scene_by_number(n)
    if not scene:
        have = len(story.scenes)
        raise ToolError(
            f"There is no scene {n}. The story has {have} scene{'s' if have != 1 else ''}."
            if have else "The story has no scenes yet."
        )
    return scene


def _outline(story: Story) -> Dict[str, Any]:
    return {
        "title": story.title, "genre": story.genre, "tone": story.tone,
        "visual_style": story.visual_style, "status": story.status,
        "characters": [{"name": c.name, "traits": c.persistent_visual_traits or derive_traits(c)}
                       for c in story.characters],
        "locations": [loc.name for loc in story.locations],
        "scenes": [{"scene_number": s.order, "summary": s.summary or s.narration[:80],
                    "setting": s.setting, "characters": s.characters, "image": s.status}
                   for s in story.scenes],
    }


# ---------------------------------------------------------------------------
# upsert helpers
# ---------------------------------------------------------------------------
def _upsert_character(story: Story, raw: Dict[str, Any]) -> Optional[Character]:
    name = _s(raw.get("name"))
    if not name:
        return None
    char = story.character(name)
    is_new = char is None
    if is_new:
        char = Character(name=name)
        story.characters.append(char)
    for field in ("description", "age", "appearance", "clothing"):
        val = _s(raw.get(field))
        if val:
            setattr(char, field, val)
    explicit = _list(raw.get("persistent_visual_traits"))
    char.persistent_visual_traits = explicit or derive_traits(
        Character(name=char.name, age=char.age, appearance=char.appearance, clothing=char.clothing)
    )
    return char


def _upsert_location(story: Story, raw: Dict[str, Any]) -> Optional[Location]:
    name = _s(raw.get("name"))
    if not name:
        return None
    loc = story.location(name)
    if loc is None:
        loc = Location(name=name)
        story.locations.append(loc)
    if _s(raw.get("description")):
        loc.description = _s(raw.get("description"))
    traits = _list(raw.get("visual_traits"))
    if traits:
        loc.visual_traits = traits
    return loc


def _build_scene(story: Story, raw: Dict[str, Any]) -> Scene:
    narration = _s(raw.get("narration"))
    scene = Scene(
        narration=narration,
        summary=_s(raw.get("summary")) or narration[:120],
        setting=_s(raw.get("setting")),
        characters=_list(raw.get("characters")),
        actions=_list(raw.get("actions")),
        emotion=_s(raw.get("emotion")),
        visual_prompt=_s(raw.get("visual_prompt")),
    )
    words = len(narration.split())
    scene.duration = float(min(12, max(5, round(words / 2.4)))) if words else 6.0
    # any character/location the scene mentions but the story doesn't know yet
    for n in scene.characters:
        if not story.character(n):
            story.characters.append(Character(name=n, persistent_visual_traits=[]))
    if scene.setting and not story.location(scene.setting):
        story.locations.append(Location(name=scene.setting))
    return scene


def _save_and_queue(story_id: str, mutate: Callable[[Story], Any]) -> Story:
    """Apply a mutation atomically; image jobs are queued by the caller afterwards."""
    story = store.update(story_id, mutate)
    if story is None:
        raise ToolError("I lost track of the story. Please start again.")
    return story


def _portraits_for_new(before_ids: set, story: Story) -> None:
    for c in story.characters:
        if c.id not in before_ids and not c.portrait_url:
            jobs.enqueue_portrait(story.id, c.id)


def _regen_scenes(story: Story, predicate: Callable[[Scene], bool], *, only_started: bool = True) -> int:
    n = 0
    for s in story.scenes:
        if predicate(s) and (not only_started or s.status != "pending"):
            n += int(jobs.enqueue_scene(story.id, s.id))
    return n


# ---------------------------------------------------------------------------
# tool executors: fn(story_id, args) -> dict
# ---------------------------------------------------------------------------
def create_story(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    story = store.get(story_id)
    is_blank = story is not None and not story.scenes and not story.characters
    if not is_blank:
        story = Story()
        store.save(story)
    story_id = story.id

    def mutate(s: Story) -> None:
        if _s(a.get("title")):
            s.title = _s(a.get("title"))
        for k in ("genre", "tone", "language"):
            if _s(a.get(k)):
                setattr(s, k, _s(a.get(k)))
        if _s(a.get("visual_style")):
            s.visual_style = _s(a.get("visual_style"))

    story = _save_and_queue(story_id, mutate)
    return {"ok": True, "story_id": story.id, "new_story": not is_blank, "outline": _outline(story)}


def add_story_content(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    narration = _s(a.get("narration_text"))
    raw_scenes = a.get("scenes") or []
    if not narration and not raw_scenes:
        raise ToolError("I didn't catch any story content to add.")
    added: List[int] = []
    before: set = set()

    def mutate(s: Story) -> None:
        before.update(c.id for c in s.characters)
        if narration:
            s.narration = f"{s.narration}\n{narration}".strip()
        for raw in a.get("characters") or []:
            if isinstance(raw, dict):
                _upsert_character(s, raw)
        for raw in a.get("locations") or []:
            if isinstance(raw, dict):
                _upsert_location(s, raw)
        scene_dicts = [r for r in raw_scenes if isinstance(r, dict)]
        if not scene_dicts and narration:  # the agent gave prose but no scene split: keep the words
            scene_dicts = [{"narration": narration, "summary": narration.split(". ")[0][:120]}]
        for raw in scene_dicts:
            if len(s.scenes) >= MAX_SCENES:
                raise ToolError(f"This story already has {MAX_SCENES} scenes, which is the most I can illustrate. "
                                "Start a new story to keep going.")
            scene = _build_scene(s, raw)
            scene.order = len(s.scenes) + 1
            s.scenes.append(scene)
            added.append(scene.order)
        # first real content names the story if the agent hasn't
        if s.title == "Untitled Story" and _s(a.get("title")):
            s.title = _s(a.get("title"))

    story = _save_and_queue(story_id, mutate)
    _portraits_for_new(before, story)
    queued = 0
    if _bool(a.get("auto_generate", True)):
        for n in added:
            sc = story.scene_by_number(n)
            queued += int(jobs.enqueue_scene(story.id, sc.id))
    return {"ok": True, "added_scenes": added, "generating": queued, "total_scenes": len(story.scenes),
            "characters": [c.name for c in story.characters]}


def analyze_story(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    """Refine the structure of what has already been narrated (fix a character's
    details, retag a scene's setting/characters). It never adds or deletes scenes."""
    changed: List[str] = []
    before: set = set()

    def mutate(s: Story) -> None:
        before.update(c.id for c in s.characters)
        for raw in a.get("characters") or []:
            if isinstance(raw, dict) and (c := _upsert_character(s, raw)):
                changed.append(c.name)
        for raw in a.get("locations") or []:
            if isinstance(raw, dict):
                _upsert_location(s, raw)
        for raw in a.get("scenes") or []:
            if not isinstance(raw, dict):
                continue
            sc = _need_scene(s, raw.get("scene_number"))
            for field, conv in (("summary", _s), ("setting", _s), ("emotion", _s), ("visual_prompt", _s),
                                ("characters", _list), ("actions", _list)):
                if raw.get(field) not in (None, "", []):
                    setattr(sc, field, conv(raw.get(field)))
            changed.append(f"scene {sc.order}")

    story = _save_and_queue(story_id, mutate)
    _portraits_for_new(before, story)
    regenerated = _regen_scenes(story, lambda sc: True)  # jobs skip scenes whose prompt is unchanged
    return {"ok": True, "updated": changed, "regenerating": regenerated, "outline": _outline(story)}


def generate_scene(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    box: Dict[str, Scene] = {}

    def mutate(s: Story) -> None:
        sc = _need_scene(s, a.get("scene_number"))
        if _s(a.get("visual_prompt")):
            sc.visual_prompt = _s(a.get("visual_prompt"))
        box["s"] = sc

    story = _save_and_queue(story_id, mutate)
    queued = jobs.enqueue_scene(story.id, box["s"].id, force=_bool(a.get("force")))
    return {"ok": True, "scene_number": box["s"].order,
            "message": "Painting it now." if queued else "That scene is already illustrated and unchanged."}


def regenerate_scene(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    note = _s(a.get("revision_instruction"))
    if not note:
        raise ToolError("Tell me what should change in that scene.")
    box: Dict[str, Scene] = {}

    def mutate(s: Story) -> None:
        sc = _need_scene(s, a.get("scene_number"))
        sc.revision += 1
        sc.revision_note = f"{sc.revision_note}; {note}".strip("; ") if sc.revision_note else note
        box["s"] = sc

    story = _save_and_queue(story_id, mutate)
    jobs.enqueue_scene(story.id, box["s"].id, force=True)
    return {"ok": True, "scene_number": box["s"].order, "revision": box["s"].revision,
            "message": f"Repainting scene {box['s'].order} with your change."}


def modify_character(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    name = _s(a.get("character_name") or a.get("character_id"))
    changes = a.get("changes") if isinstance(a.get("changes"), dict) else {}
    if not name:
        raise ToolError("Which character should I change?")
    if not changes:
        raise ToolError("What should change about them?")
    box: Dict[str, Any] = {}

    def mutate(s: Story) -> None:
        char = s.character(name)
        if not char:
            known = ", ".join(c.name for c in s.characters) or "nobody yet"
            raise ToolError(f"I don't have a character called {name}. I know: {known}.")
        old_name = char.name
        for field in ("description", "age", "appearance", "clothing"):
            if _s(changes.get(field)):
                setattr(char, field, _s(changes.get(field)))
        explicit = _list(changes.get("persistent_visual_traits"))
        # recompute from the (possibly edited) fields so old values don't linger
        char.persistent_visual_traits = explicit or derive_traits(
            Character(name=char.name, age=char.age, appearance=char.appearance, clothing=char.clothing))
        for t in _list(changes.get("add_traits")):
            if t.lower() not in [x.lower() for x in char.persistent_visual_traits]:
                char.persistent_visual_traits.append(t)
        drop = [t.lower() for t in _list(changes.get("remove_traits"))]
        char.persistent_visual_traits = [t for t in char.persistent_visual_traits if t.lower() not in drop]
        new_name = _s(changes.get("name"))
        if new_name and new_name != old_name:
            char.name = new_name
            for sc in s.scenes:
                sc.characters = [new_name if n.lower() == old_name.lower() else n for n in sc.characters]
        char.portrait_url = None
        box["char"] = char
        box["old"] = old_name

    story = _save_and_queue(story_id, mutate)
    char = box["char"]
    jobs.enqueue_portrait(story.id, char.id)
    affected = _regen_scenes(story, lambda sc: char.name.lower() in [n.lower() for n in sc.characters])
    return {"ok": True, "character": char.name, "traits": char.persistent_visual_traits,
            "scenes_being_repainted": affected}


def modify_story_style(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(s: Story) -> None:
        if _s(a.get("visual_style")):
            s.visual_style = _s(a.get("visual_style"))
        if _s(a.get("style_changes")):
            s.visual_style = f"{s.visual_style}, {_s(a.get('style_changes'))}"
        if _s(a.get("tone")):
            s.tone = _s(a.get("tone"))
        if _s(a.get("genre")):
            s.genre = _s(a.get("genre"))

    story = _save_and_queue(story_id, mutate)
    regenerated = _regen_scenes(story, lambda sc: True)
    return {"ok": True, "visual_style": story.visual_style, "tone": story.tone,
            "scenes_being_repainted": regenerated}


def add_scene(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    if not (_s(a.get("narration")) or _s(a.get("summary")) or _s(a.get("visual_prompt"))):
        raise ToolError("Tell me what happens in the new scene.")
    box: Dict[str, Scene] = {}
    before: set = set()

    def mutate(s: Story) -> None:
        if len(s.scenes) >= MAX_SCENES:
            raise ToolError(f"This story already has {MAX_SCENES} scenes, which is the most I can illustrate.")
        before.update(c.id for c in s.characters)
        sc = _build_scene(s, a)
        pos = a.get("position")
        idx = len(s.scenes) if pos in (None, "") else max(0, min(len(s.scenes), _int(pos, "position") - 1))
        s.scenes.insert(idx, sc)
        s.renumber()
        box["s"] = sc

    story = _save_and_queue(story_id, mutate)
    _portraits_for_new(before, story)
    jobs.enqueue_scene(story.id, box["s"].id)
    return {"ok": True, "scene_number": box["s"].order, "total_scenes": len(story.scenes)}


def remove_scene(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    story = store.get(story_id)
    if not story:
        raise ToolError("There is no story yet.")
    sc = _need_scene(story, a.get("scene_number"))
    if not _bool(a.get("confirmed")):
        return {"ok": False, "needs_confirmation": True,
                "message": f"Ask the user to confirm deleting scene {sc.order}: {sc.summary[:80]!r}. "
                           "Call remove_scene again with confirmed=true only after they say yes."}

    def mutate(s: Story) -> None:
        target = _need_scene(s, a.get("scene_number"))
        s.scenes = [x for x in s.scenes if x.id != target.id]
        s.renumber()

    story = _save_and_queue(story_id, mutate)
    return {"ok": True, "total_scenes": len(story.scenes)}


def reorder_scene(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    def mutate(s: Story) -> None:
        sc = _need_scene(s, a.get("scene_number"))
        new_pos = max(1, min(len(s.scenes), _int(a.get("new_position"), "the new position")))
        s.scenes = [x for x in s.scenes if x.id != sc.id]
        s.scenes.insert(new_pos - 1, sc)
        s.renumber()

    story = _save_and_queue(story_id, mutate)
    return {"ok": True, "order": [{"scene_number": x.order, "summary": x.summary[:60]} for x in story.scenes]}


def preview_story(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    story = store.get(story_id)
    if not story:
        raise ToolError("There is no story yet.")
    return {"ok": True, "outline": _outline(story)}


def play_story(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    story = store.get(story_id)
    if not story or not story.scenes:
        raise ToolError("There are no scenes to play yet.")
    ready = sum(1 for s in story.scenes if s.status == "ready")
    start = _int(a.get("from_scene") or 1, "the start scene")
    return {"ok": True, "ui_action": "play", "from_scene": start, "ready_scenes": ready,
            "total_scenes": len(story.scenes),
            "message": "Starting playback." if ready == len(story.scenes)
            else f"Playing what's ready ({ready} of {len(story.scenes)} scenes illustrated so far)."}


def export_story(story_id: str, a: Dict[str, Any]) -> Dict[str, Any]:
    from . import render

    story = store.get(story_id)
    if not story or not story.scenes:
        raise ToolError("There is nothing to export yet.")
    if not any(s.status == "ready" for s in story.scenes):
        raise ToolError("No scene is illustrated yet. Give me a moment to finish painting first.")
    render.enqueue_export(story.id)
    return {"ok": True, "message": "Rendering your illustrated story now. I'll tell you when the video is ready."}


REGISTRY: Dict[str, Callable[[str, Dict[str, Any]], Dict[str, Any]]] = {
    "create_story": create_story, "add_story_content": add_story_content, "analyze_story": analyze_story,
    "generate_scene": generate_scene, "regenerate_scene": regenerate_scene, "modify_character": modify_character,
    "modify_story_style": modify_story_style, "add_scene": add_scene, "remove_scene": remove_scene,
    "reorder_scene": reorder_scene, "preview_story": preview_story, "play_story": play_story,
    "export_story": export_story,
}


def execute(name: str, story_id: str, arguments: Any) -> Dict[str, Any]:
    """Run a tool. Never raises: the agent always gets a result it can speak from."""
    fn = REGISTRY.get(name)
    if fn is None:
        return {"ok": False, "error": f"I don't have a tool called {name}."}
    if isinstance(arguments, str):
        import json

        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return {"ok": False, "error": "I couldn't read those tool arguments."}
    args = arguments if isinstance(arguments, dict) else {}
    log.info("tool.request name=%s story=%s", name, story_id)
    try:
        result = fn(story_id, args)
        log.info("tool.done name=%s ok=%s", name, result.get("ok"))
        return result
    except ToolError as exc:
        log.info("tool.refused name=%s reason=%s", name, exc)
        return {"ok": False, "error": str(exc)}
    except Exception:  # noqa: BLE001
        log.exception("tool.error name=%s", name)
        return {"ok": False, "error": "Something went wrong on my side. Please try that again."}


# ---------------------------------------------------------------------------
# JSON-schema definitions (AssemblyAI Voice Agent API tool format)
# ---------------------------------------------------------------------------
_CHARACTER = {
    "type": "object",
    "description": "A character. Always fill appearance and clothing with concrete visual details so every illustration matches.",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string", "description": "Who they are, e.g. 'a curious young girl'."},
        "age": {"type": "string", "description": "e.g. 'about 8'."},
        "appearance": {"type": "string", "description": "Skin, hair, build, e.g. 'dark skin, long black braids'."},
        "clothing": {"type": "string", "description": "Outfit and accessories with colours, e.g. 'blue dress, brown sandals, yellow bracelet'."},
    },
    "required": ["name"],
}
_LOCATION = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "visual_traits": {"type": "array", "items": {"type": "string"}, "description": "Concrete visuals, e.g. ['stone huts', 'misty mountains']."},
    },
    "required": ["name"],
}
_SCENE = {
    "type": "object",
    "properties": {
        "narration": {"type": "string", "description": "The storyteller's own words for this scene, lightly cleaned up. Do not invent."},
        "summary": {"type": "string", "description": "One short sentence."},
        "setting": {"type": "string", "description": "Location name."},
        "characters": {"type": "array", "items": {"type": "string"}, "description": "Names of characters visible in the scene."},
        "actions": {"type": "array", "items": {"type": "string"}},
        "emotion": {"type": "string"},
        "visual_prompt": {"type": "string", "description": "What the illustration should show: composition, lighting, key objects."},
    },
    "required": ["narration", "summary", "visual_prompt"],
}
_N = {"type": "integer", "description": "Scene number as the user says it (1 = first scene)."}


def _fn(name: str, description: str, properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []}}


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    _fn("create_story", "Set the story's title, genre, tone, language or visual style. Use at the start, or when the user asks to start a brand-new story.",
        {"title": {"type": "string"}, "genre": {"type": "string"}, "tone": {"type": "string"},
         "visual_style": {"type": "string", "description": "e.g. 'soft watercolour picture book'."},
         "language": {"type": "string", "description": "Language code, e.g. 'en'."}}),
    _fn("add_story_content",
        "Record NEW story narration the user just spoke and split it into structured scenes. Call this every time the user narrates story content (not for instructions). New scenes are illustrated automatically.",
        {"narration_text": {"type": "string", "description": "The narration just spoken."},
         "title": {"type": "string", "description": "A title if one is obvious and the story has none."},
         "characters": {"type": "array", "items": _CHARACTER},
         "locations": {"type": "array", "items": _LOCATION},
         "scenes": {"type": "array", "items": _SCENE, "description": "One entry per distinct visual moment."},
         "auto_generate": {"type": "boolean"}},
        ["narration_text", "scenes"]),
    _fn("analyze_story", "Refine the structure of the existing story: correct character or location details, or re-tag an existing scene's setting/characters/visuals. Never adds or removes scenes.",
        {"characters": {"type": "array", "items": _CHARACTER}, "locations": {"type": "array", "items": _LOCATION},
         "scenes": {"type": "array", "items": {"type": "object", "properties": {
             "scene_number": _N, "summary": {"type": "string"}, "setting": {"type": "string"},
             "characters": {"type": "array", "items": {"type": "string"}}, "actions": {"type": "array", "items": {"type": "string"}},
             "emotion": {"type": "string"}, "visual_prompt": {"type": "string"}}, "required": ["scene_number"]}}}),
    _fn("generate_scene", "Illustrate a scene that has no illustration yet or failed. Skips scenes already illustrated unless force is true.",
        {"scene_number": _N, "visual_prompt": {"type": "string"}, "force": {"type": "boolean"}}, ["scene_number"]),
    _fn("regenerate_scene", "Repaint an existing scene after the user asks for a change to it.",
        {"scene_number": _N, "revision_instruction": {"type": "string", "description": "What to change, e.g. 'make the tree blue and more magical'."}},
        ["scene_number", "revision_instruction"]),
    _fn("modify_character", "Change a character's persistent look (age, hair, clothing, name...). Every scene they appear in is repainted to match.",
        {"character_name": {"type": "string"},
         "changes": {"type": "object", "properties": {
             "name": {"type": "string"}, "description": {"type": "string"}, "age": {"type": "string"},
             "appearance": {"type": "string", "description": "Full updated appearance (replaces the old one)."},
             "clothing": {"type": "string", "description": "Full updated outfit (replaces the old one)."},
             "add_traits": {"type": "array", "items": {"type": "string"}},
             "remove_traits": {"type": "array", "items": {"type": "string"}}}}},
        ["character_name", "changes"]),
    _fn("modify_story_style", "Change the visual style or tone of the whole story, e.g. 'more mysterious'. All scenes are repainted.",
        {"visual_style": {"type": "string", "description": "Replace the style entirely."},
         "style_changes": {"type": "string", "description": "Add to the current style, e.g. 'misty, mysterious lighting'."},
         "tone": {"type": "string"}, "genre": {"type": "string"}}),
    _fn("add_scene", "Insert a new scene, at the end unless a position is given.",
        {**_SCENE["properties"], "position": {"type": "integer", "description": "Where it goes (1 = first). Omit for the end."}},
        ["narration"]),
    _fn("remove_scene", "Delete a scene. ALWAYS ask the user to confirm first, then call with confirmed=true.",
        {"scene_number": _N, "confirmed": {"type": "boolean"}}, ["scene_number"]),
    _fn("reorder_scene", "Move a scene to a new position.", {"scene_number": _N, "new_position": {"type": "integer"}},
        ["scene_number", "new_position"]),
    _fn("preview_story", "Get the current title, characters and scene list to describe the story or answer questions about it.", {}),
    _fn("play_story", "Play the illustrated story on screen with captions and narration.",
        {"from_scene": {"type": "integer", "description": "Scene to start from (default 1)."}}),
    _fn("export_story", "Render the finished illustrated story as a downloadable video.", {}),
]
