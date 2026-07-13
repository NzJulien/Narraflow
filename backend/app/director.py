"""
Director Agent
--------------
Turns an idea into a story plan: title, genre, tone, and a pacing arc
(one entry per scene - "setup", "rising", "twist", "climax",
"resolution" - repeated/trimmed to fit scene_count). Every other agent
in the pipeline (Writer, Memory, Artist, Cinematographer) reads its
pacing cue for a scene from this arc, so the Director is the only place
that decides story *shape*.

NOTE: this module wasn't part of the code bundle that was pasted in for
this pass - it's reconstructed here to match the exact interface
orchestrator.py already calls (plan_story(idea, scene_count) -> plan
with "title"/"genre"/"tone"/"scenes"/"arc"). If a different original
director.py exists in your repo, drop it in here instead - nothing else
needs to change.

DIRECTOR_BACKEND=fireworks uses a Fireworks chat completion to plan;
default "mock" is a deterministic local planner. Same defensive
contract as the rest of the pipeline: never raises.
"""

import hashlib
import json
import os
from typing import List

DIRECTOR_BACKEND = os.environ.get("DIRECTOR_BACKEND", "mock").strip().lower()
FIREWORKS_TEXT_MODEL = os.environ.get(
    "FIREWORKS_TEXT_MODEL", "accounts/fireworks/models/llama-v3p1-70b-instruct"
)
FIREWORKS_BASE_URL = os.environ.get("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")
_api_key = os.environ.get("FIREWORKS_API_KEY", "").strip()

if DIRECTOR_BACKEND == "fireworks" and not _api_key:
    print("[director] FIREWORKS_API_KEY not set; falling back to mock planning.")
    DIRECTOR_BACKEND = "mock"

BASE_ARC = ["setup", "rising", "twist", "climax", "resolution"]
GENRES = ["Adventure", "Mystery", "Sci-Fi", "Fantasy", "Drama"]
TONES = ["Whimsical", "Epic", "Heartfelt", "Melancholic", "Dark"]


def _fallback_arc(scene_count: int) -> List[str]:
    arc = list(BASE_ARC)
    while len(arc) < scene_count:
        arc.insert(-1, "rising")  # pad the middle, always end on resolution
    return arc[:scene_count]


def _local_plan(idea: str, scene_count: int) -> dict:
    seed = int(hashlib.sha1(idea.encode()).hexdigest(), 16)
    title = idea.strip().split(".")[0][:60] or "Untitled Story"
    return {
        "title": title,
        "genre": GENRES[seed % len(GENRES)],
        "tone": TONES[(seed // 7) % len(TONES)],
        "scenes": scene_count,
        "arc": _fallback_arc(scene_count),
    }


def _fireworks_plan(idea: str, scene_count: int) -> dict:
    import requests

    prompt = (
        "You are a story director. Given a one-line idea, respond ONLY with JSON of "
        'the shape {"title": str, "genre": str, "tone": str, "arc": [list of exactly '
        f'{scene_count} pacing words, each one of setup/rising/twist/climax/resolution]}}. '
        f"Idea: {idea}"
    )
    resp = requests.post(
        f"{FIREWORKS_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
        json={"model": FIREWORKS_TEXT_MODEL, "max_tokens": 300,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    data["scenes"] = scene_count
    if len(data.get("arc", [])) != scene_count:
        data["arc"] = _fallback_arc(scene_count)
    return data


def plan_story(idea: str, scene_count: int = 5) -> dict:
    """Never raises - degrades to a deterministic local plan on any failure."""
    if DIRECTOR_BACKEND == "fireworks":
        try:
            return _fireworks_plan(idea, scene_count)
        except Exception as exc:  # noqa: BLE001
            print(f"[director] Fireworks planning failed ({exc}); using local plan.")
    return _local_plan(idea, scene_count)
