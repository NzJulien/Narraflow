"""Continuity-aware prompt composition.

Every image prompt is built from the structured story state, never written by
hand per scene. Characters' persistent visual traits, the location's visual
traits and the story-wide style are injected automatically, so "Amara" is the
same girl in scene 1 and scene 6.
"""

from __future__ import annotations

import hashlib
import re
from typing import List

from .models import Character, Location, Scene, Story

NEGATIVE_HINT = (
    "no text, no captions, no watermark, consistent character design across the series"
)


def _split_traits(text: str) -> List[str]:
    parts = re.split(r"[,;]| and ", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def derive_traits(char: Character) -> List[str]:
    """Persistent traits = age + appearance + clothing (+ any explicit ones)."""
    traits: List[str] = []
    if char.age:
        traits.append(char.age if "old" in char.age or "year" in char.age else f"{char.age}")
    traits += _split_traits(char.appearance)
    traits += _split_traits(char.clothing)
    seen, out = set(), []
    for t in traits + list(char.persistent_visual_traits):
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def character_phrase(char: Character) -> str:
    traits = char.persistent_visual_traits or derive_traits(char)
    base = char.description.strip() or char.name
    return f"{char.name} ({base}; {', '.join(traits)})" if traits else f"{char.name} ({base})"


def location_phrase(loc: Location) -> str:
    traits = ", ".join(loc.visual_traits)
    desc = loc.description.strip()
    bits = [b for b in (desc, traits) if b]
    return f"{loc.name}: {'; '.join(bits)}" if bits else loc.name


def compose_scene_prompt(story: Story, scene: Scene) -> str:
    parts: List[str] = [story.visual_style]
    if story.tone:
        parts.append(f"{story.tone} mood")

    loc = story.location(scene.setting) if scene.setting else None
    if loc:
        parts.append("setting - " + location_phrase(loc))
    elif scene.setting:
        parts.append(f"setting - {scene.setting}")

    for name in scene.characters:
        char = story.character(name)
        parts.append("character - " + (character_phrase(char) if char else name))

    if scene.actions:
        parts.append("action - " + "; ".join(scene.actions))
    if scene.emotion:
        parts.append(f"emotion - {scene.emotion}")
    if scene.visual_prompt:
        parts.append(scene.visual_prompt.strip())
    elif scene.summary:
        parts.append(scene.summary.strip())
    if scene.revision_note:
        parts.append(f"revision - {scene.revision_note.strip()}")
    parts.append(NEGATIVE_HINT)
    return ". ".join(p.rstrip(". ") for p in parts if p)


def compose_portrait_prompt(story: Story, char: Character) -> str:
    return (
        f"{story.visual_style}. character reference portrait, full figure, plain soft background - "
        f"{character_phrase(char)}. {NEGATIVE_HINT}"
    )


def prompt_hash(prompt: str) -> str:
    return hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
