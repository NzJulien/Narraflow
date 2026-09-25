"""Structured story model. A story is never one big string: it is characters,
locations and ordered scenes, each carrying the visual facts needed to keep
illustrations consistent."""

from __future__ import annotations

import time
import uuid
from typing import List, Optional

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    # Story ids are the only thing protecting a story from other visitors, so keep them unguessable.
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> float:
    return time.time()


class Character(BaseModel):
    id: str = Field(default_factory=lambda: new_id("chr"))
    name: str
    description: str = ""
    age: str = ""
    appearance: str = ""
    clothing: str = ""
    # Short, concrete visual phrases re-injected into EVERY image prompt this
    # character appears in ("long black braids", "blue dress", ...).
    persistent_visual_traits: List[str] = Field(default_factory=list)
    portrait_url: Optional[str] = None


class Location(BaseModel):
    id: str = Field(default_factory=lambda: new_id("loc"))
    name: str
    description: str = ""
    visual_traits: List[str] = Field(default_factory=list)


class Scene(BaseModel):
    id: str = Field(default_factory=lambda: new_id("scn"))
    order: int = 0
    narration: str = ""
    summary: str = ""
    setting: str = ""  # location name
    characters: List[str] = Field(default_factory=list)  # character names
    actions: List[str] = Field(default_factory=list)
    emotion: str = ""
    visual_prompt: str = ""
    image_url: Optional[str] = None
    audio_url: Optional[str] = None
    duration: float = 6.0
    revision: int = 0
    revision_note: str = ""
    prompt_hash: str = ""  # hash of the prompt behind image_url; lets us skip redundant regeneration
    # pending -> generating -> ready | failed
    status: str = "pending"
    error: str = ""


class Story(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sty"))
    title: str = "Untitled Story"
    genre: str = ""
    tone: str = ""
    language: str = "en"
    visual_style: str = "warm hand-painted children's picture book illustration"
    narration: str = ""
    characters: List[Character] = Field(default_factory=list)
    locations: List[Location] = Field(default_factory=list)
    scenes: List[Scene] = Field(default_factory=list)
    # draft | rendering | complete
    status: str = "draft"
    export_url: Optional[str] = None
    export_error: str = ""
    demo: bool = False
    version: int = 0  # bumped on every change; drives the live-update stream
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)

    # ---- lookups -------------------------------------------------------
    def character(self, name: str) -> Optional[Character]:
        key = (name or "").strip().lower()
        return next((c for c in self.characters if c.name.lower() == key), None)

    def location(self, name: str) -> Optional[Location]:
        key = (name or "").strip().lower()
        return next((loc for loc in self.locations if loc.name.lower() == key), None)

    def scene_by_number(self, number: int) -> Optional[Scene]:
        return next((s for s in self.scenes if s.order == number), None)

    def renumber(self) -> None:
        """Numbering follows list position; callers reorder the list, this just labels it."""
        for i, scene in enumerate(self.scenes, start=1):
            scene.order = i
