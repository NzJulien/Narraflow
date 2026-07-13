"""
Memory Agent
------------
Tracks persistent world state per story: which characters, locations,
and items have been introduced so far, so later scenes (and the Writer
agent's prompt) build on what already exists instead of each scene
starting from a blank slate - this is what makes "Amara" from scene 1
still be "Amara" in scene 4 instead of a new name every time, and what
the frontend's world panel / relationship graph visualize.

NOTE: reconstructed here to match the exact interface orchestrator.py
already calls (new_world, get_world, update_memory) - it wasn't part of
the pasted bundle. Drop in your original module instead if you have it;
nothing else needs to change.

Storage is a plain in-process dict keyed by story_id - fine for a
single-process hackathon demo; swap for Redis/a DB if this ever needs
to survive a restart or run across multiple workers.
"""

from typing import Dict

_worlds: Dict[str, dict] = {}


def new_world(story_id: str) -> dict:
    _worlds[story_id] = {"characters": [], "locations": [], "items": []}
    return _worlds[story_id]


def get_world(story_id: str) -> dict:
    return _worlds.get(story_id) or new_world(story_id)


def update_memory(story_id: str, scene: dict) -> dict:
    """
    Folds a scene's introduced characters/locations and gained/lost
    items into the running world state. Never raises - a malformed
    scene dict just contributes nothing new rather than crashing the
    pipeline.
    """
    world = get_world(story_id)
    for c in scene.get("characters_introduced", []) or []:
        if c not in world["characters"]:
            world["characters"].append(c)
    for loc in scene.get("locations_introduced", []) or []:
        if loc not in world["locations"]:
            world["locations"].append(loc)
    for item in (scene.get("items_gained", {}) or {}).keys():
        if item not in world["items"]:
            world["items"].append(item)
    for item in (scene.get("items_lost", {}) or {}).keys():
        if item in world["items"]:
            world["items"].remove(item)
    return world
