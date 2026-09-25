"""
Narrator Agent
--------------
Turns raw narrated speech into a faithful, readable story - incrementally,
as new finalized transcript segments arrive, rather than regenerating from
scratch on every call. This is the engine behind /narration/segment and
/narration/stop in main.py.

Same defensive contract as the rest of the pipeline (writer.py, director.py,
etc.): NARRATOR_BACKEND toggles "mock" (default, deterministic, offline) vs
"fireworks" (hosted LLM), and any failure degrades to the mock path rather
than raising or losing the narrator's words.

The single most important rule here, straight from the product spec: this
must SHAPE the story, never INVENT it. The mock path enforces that
structurally - it only ever cleans up and appends the narrator's own words,
so it is physically incapable of fabricating a detail. The fireworks path
enforces it via an explicit system prompt plus the same "integrate, don't
replace" call shape, so a bad completion can drift in style but can't
silently swap out what was actually said without violating its instructions.

Context management: each call receives only `story_so_far` (capped to the
trailing ~4000 chars once a story gets long - earlier parts are already
written and don't need to be re-sent) plus the *new* segment. The full
transcript is never resent - it's persisted separately in storage.py.
"""

import os
import re
from typing import Optional

NARRATOR_BACKEND = os.environ.get("NARRATOR_BACKEND", "mock").strip().lower()
FIREWORKS_TEXT_MODEL = os.environ.get(
    "FIREWORKS_TEXT_MODEL", "accounts/fireworks/models/llama-v3p1-70b-instruct"
)
FIREWORKS_BASE_URL = os.environ.get("FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1")
_api_key = os.environ.get("FIREWORKS_API_KEY", "").strip()

if NARRATOR_BACKEND == "fireworks" and not _api_key:
    print("[narrator] FIREWORKS_API_KEY not set; falling back to mock (cleanup-only) mode.")
    NARRATOR_BACKEND = "mock"

STORY_CONTEXT_CHARS = 4000

# Eats an optional leading/trailing comma along with the filler itself, so
# "her parents, you know." doesn't leave a dangling ", ." behind.
FILLER_PATTERN = re.compile(
    r"\s*,?\s*\b(um+|uh+|erm+|you know|i mean|like)\b\s*,?\s*", re.IGNORECASE
)
WHITESPACE_PATTERN = re.compile(r"[ \t]+")
STRAY_PUNCT_PATTERN = re.compile(r"\s*,\s*(?=[.!?])")  # ", ." -> "."
SPACE_BEFORE_PUNCT_PATTERN = re.compile(r"\s+([.,!?])")
DOUBLE_PUNCT_PATTERN = re.compile(r"([.,!?])[.,!?]+")


def _clean_segment(text: str) -> str:
    """Strips filler words and normalizes whitespace/capitalization. Never removes or
    alters actual content words - only disfluencies and spacing."""
    cleaned = FILLER_PATTERN.sub(" ", text)
    cleaned = STRAY_PUNCT_PATTERN.sub("", cleaned)
    cleaned = SPACE_BEFORE_PUNCT_PATTERN.sub(r"\1", cleaned)
    cleaned = DOUBLE_PUNCT_PATTERN.sub(r"\1", cleaned)
    cleaned = WHITESPACE_PATTERN.sub(" ", cleaned).strip()
    cleaned = cleaned.strip(",")
    if cleaned and cleaned[0].isalpha():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _mock_update(story_so_far: str, new_segment: str) -> str:
    cleaned = _clean_segment(new_segment)
    if not cleaned:
        return story_so_far
    if not story_so_far:
        return cleaned
    # Ensure the story so far ends with sentence-ending punctuation before appending.
    joiner = "" if story_so_far.rstrip()[-1:] in ".!?" else "."
    return f"{story_so_far.rstrip()}{joiner} {cleaned}"


SYSTEM_PROMPT = (
    "You are NarraFlow's narration assistant. The user is narrating a TRUE, personal "
    "story out loud. You receive the story as written so far, plus a new segment of "
    "what they just said. Your only job is to integrate the new segment into the "
    "story, improving grammar, flow, and readability.\n\n"
    "STRICT RULES:\n"
    "- Never invent or add any name, place, date, event, relationship, object, quote, "
    "or emotion that was not stated.\n"
    "- Preserve every fact and the chronology exactly as narrated.\n"
    "- Preserve the narrator's own voice - do not over-polish, do not add dramatic or "
    "flowery language they didn't use.\n"
    "- Do not repeat information already present in the story so far.\n"
    "- Do not add a new introduction or conclusion - just extend the story naturally.\n"
    "- Output ONLY the full updated story text. No headers, no commentary, no markdown."
)


def _fireworks_update(story_so_far: str, new_segment: str, language: str) -> str:
    import requests

    context = story_so_far
    truncated = False
    if len(context) > STORY_CONTEXT_CHARS:
        context = context[-STORY_CONTEXT_CHARS:]
        truncated = True

    context_note = (
        "(earlier parts of the story already exist and are omitted here - do not "
        "try to reconstruct or repeat them)\n" if truncated else ""
    )
    prompt = (
        f"{context_note}Story so far:\n{context or '(nothing yet)'}\n\n"
        f"New narrated segment to integrate:\n{new_segment}\n\n"
        f"Respond in this language: {language}."
    )
    resp = requests.post(
        f"{FIREWORKS_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
        json={
            "model": FIREWORKS_TEXT_MODEL,
            "max_tokens": 900,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return text or _mock_update(story_so_far, new_segment)


def update_story(story_so_far: str, new_segment: str, language: str = "en") -> str:
    """Never raises - degrades to the cleanup-only mock update on any failure, which
    means a flaky LLM call never loses or corrupts what the narrator already said."""
    if not new_segment or not new_segment.strip():
        return story_so_far
    if NARRATOR_BACKEND == "fireworks":
        try:
            return _fireworks_update(story_so_far, new_segment, language)
        except Exception as exc:  # noqa: BLE001
            print(f"[narrator] Fireworks story update failed ({exc}); using cleanup-only fallback.")
    return _mock_update(story_so_far, new_segment)


def generate_title(story_text: str, transcript: Optional[str] = None) -> str:
    """Never raises - degrades to a deterministic title derived from the opening words."""
    source = (story_text or transcript or "").strip()
    if NARRATOR_BACKEND == "fireworks" and source:
        try:
            return _fireworks_title(source)
        except Exception as exc:  # noqa: BLE001
            print(f"[narrator] Fireworks title generation failed ({exc}); using local title.")
    return _local_title(source)


def _local_title(source: str) -> str:
    if not source:
        return "Untitled Story"
    first_sentence = re.split(r"(?<=[.!?])\s", source, maxsplit=1)[0]
    words = first_sentence.split()[:7]
    if not words:
        return "Untitled Story"
    return " ".join(w.capitalize() if w.islower() else w for w in words).rstrip(".!?,")


def _fireworks_title(source: str) -> str:
    import requests

    resp = requests.post(
        f"{FIREWORKS_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
        json={
            "model": FIREWORKS_TEXT_MODEL,
            "max_tokens": 20,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Write a short, specific title (max 7 words) for the following true "
                        "personal story. Respond with ONLY the title, no quotes, no punctuation "
                        "at the end."
                    ),
                },
                {"role": "user", "content": source[:2000]},
            ],
        },
        timeout=20,
    )
    resp.raise_for_status()
    title = resp.json()["choices"][0]["message"]["content"].strip().strip('"')
    return title or _local_title(source)
