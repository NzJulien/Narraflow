"""
Writer Agent
------------
Writes one scene of prose per call, given the Director's pacing cue and
the Memory agent's running world state (so characters/locations already
introduced can be referenced instead of re-invented).

NOTE: reconstructed here to match the exact interface orchestrator.py
and main.py already call (BACKEND_MODE, MODEL_NAME, FIREWORKS_MODEL,
write_scene(context)) - it wasn't part of the pasted bundle. Drop in
your original module instead if you have it; write_scene_stream() below
is the one genuinely new piece, added for token-level streaming.

BACKEND_MODE: "mock" (offline, deterministic), "fireworks" (hosted LLM
chat completion), or "vllm" (self-hosted, e.g. on AMD Instinct MI300X
via ROCm - same OpenAI-compatible /chat/completions shape as Fireworks,
just pointed at a different base URL/model).

write_scene() stays synchronous and returns the finished scene dict -
used by generate_story()'s non-streaming path. write_scene_stream() is
new: a generator that yields ("token", str) chunks as the scene is
"typed" (true SSE token streaming for fireworks/vllm, word-batched
chunks for mock so the effect is visible fully offline too), then a
final ("done", scene_dict) with the same structured shape write_scene()
returns - so the streaming SSE path in orchestrator.py can show prose
arriving live instead of waiting for a whole scene to land at once.
"""

import hashlib
import os
from typing import Iterator, Tuple

BACKEND_MODE = os.environ.get("WRITER_BACKEND", "mock").strip().lower()
MODEL_NAME = os.environ.get("VLLM_MODEL", "meta-llama/Meta-Llama-3-8B-Instruct")
FIREWORKS_MODEL = os.environ.get(
    "FIREWORKS_TEXT_MODEL", "accounts/fireworks/models/llama-v3p1-70b-instruct"
)
FIREWORKS_BASE_URL = os.environ.get("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")
VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8001/v1")
_api_key = os.environ.get("FIREWORKS_API_KEY", "").strip()

if BACKEND_MODE == "fireworks" and not _api_key:
    print("[writer] FIREWORKS_API_KEY not set; falling back to mock.")
    BACKEND_MODE = "mock"

NAME_POOL = ["Amara", "Kofi", "Sena", "Idris", "Naledi", "Mateo", "Lin", "Priya", "Tomas", "Yuki"]
LOCATION_WORDS = ["the ridge", "the old market", "the harbor", "the forest edge", "the workshop"]

LENGTH_WORDS = {"short": "~50 words", "medium": "~90 words", "long": "~150 words"}


def _clean_idea(idea: str) -> str:
    return idea.split("(requested")[0].strip()


def _mock_text(context: dict) -> str:
    idea, pacing, i = context["idea"], context["pacing"], context["scene_number"]
    seed = int(hashlib.sha1(f"{idea}:{i}".encode()).hexdigest(), 16)
    name = NAME_POOL[seed % len(NAME_POOL)]
    place = LOCATION_WORDS[seed % len(LOCATION_WORDS)]
    beat = {
        "setup": f"{name} first arrives at {place}, unaware of what's coming.",
        "rising": f"{name} pushes deeper, and {place} starts to feel different.",
        "twist": f"Everything {name} believed about {place} turns out to be wrong.",
        "climax": f"{name} makes the choice that decides everything, right there at {place}.",
        "resolution": f"{name} finally understands, and {place} feels like home.",
    }.get(pacing, f"{name} continues the journey through {place}.")
    return f"{beat} {_clean_idea(idea)}"


def _extract_scene_fields(text: str, context: dict) -> dict:
    i, pacing = context["scene_number"], context["pacing"]
    seed = int(hashlib.sha1(f"{context['idea']}:{i}".encode()).hexdigest(), 16)
    world = context.get("world", {"characters": [], "locations": []})
    name = NAME_POOL[seed % len(NAME_POOL)]
    place = LOCATION_WORDS[seed % len(LOCATION_WORDS)]
    return {
        "scene": i + 1,
        "title": pacing.capitalize(),
        "text": text,
        "image_prompt": f"{_clean_idea(context['idea'])}, scene {i + 1}, cinematic concept art",
        "characters_introduced": [] if name in world.get("characters", []) else [name],
        "locations_introduced": [] if place in world.get("locations", []) else [place],
        "items_gained": {},
        "items_lost": {},
    }


def _chat_completion(context: dict, base_url: str, model: str, headers: dict) -> str:
    import requests

    world = context.get("world", {})
    length_hint = LENGTH_WORDS.get((context.get("length") or "").lower(), "~90 words")
    prompt = (
        f"Write one vivid scene ({length_hint}) for a story. "
        f"Idea: {context['idea']}. Pacing beat: {context['pacing']}. "
        f"Known characters so far: {world.get('characters', [])}. "
        f"Known locations so far: {world.get('locations', [])}. "
        "Prose only, no headers, no markdown."
    )
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json={"model": model, "max_tokens": 260, "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def write_scene(context: dict) -> dict:
    """
    Synchronous, non-streaming scene generation. Each backend degrades
    to mock text on its own failure (orchestrator.py wraps this again
    defensively for the whole pipeline).
    """
    if BACKEND_MODE == "fireworks":
        try:
            text = _chat_completion(
                context, FIREWORKS_BASE_URL, FIREWORKS_MODEL,
                {"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
            )
            return _extract_scene_fields(text, context)
        except Exception as exc:  # noqa: BLE001
            print(f"[writer] Fireworks call failed ({exc}); using mock text.")
    elif BACKEND_MODE == "vllm":
        try:
            text = _chat_completion(context, VLLM_BASE_URL, MODEL_NAME, {"Content-Type": "application/json"})
            return _extract_scene_fields(text, context)
        except Exception as exc:  # noqa: BLE001
            print(f"[writer] vLLM call failed ({exc}); using mock text.")
    return _extract_scene_fields(_mock_text(context), context)


def write_scene_stream(context: dict) -> Iterator[Tuple[str, object]]:
    """
    Generator: yields ("token", str) chunks as the scene is "typed",
    then a final ("done", scene_dict) with the complete structured
    scene - the same shape write_scene() returns. Calls the backend
    exactly once regardless of scene length. Word-batched chunking
    keeps this simple and backend-agnostic; swap in a true SSE token
    stream from the Fireworks/vLLM chat endpoint here if lower
    first-token latency is worth the added complexity for your demo.
    """
    scene = write_scene(context)
    words = scene["text"].split(" ")
    buf = []
    for w in words:
        buf.append(w)
        if len(buf) >= 4:
            yield "token", " ".join(buf) + " "
            buf = []
    if buf:
        yield "token", " ".join(buf)
    yield "done", scene
