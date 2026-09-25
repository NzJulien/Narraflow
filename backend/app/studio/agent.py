"""AssemblyAI Voice Agent API integration (server side).

The browser talks to AssemblyAI directly over WebSocket with a short-lived,
single-use token minted here, so the permanent API key never reaches the client.
This module:
  * mints those tokens (GET https://agents.assemblyai.com/v1/token, Bearer key),
  * builds the `session.update` payload: system prompt, greeting, turn-detection
    tuned for storytellers who pause to think, and the tool schema,
  * rate-limits token minting per client so a public deployment can't be used to
    burn the API key's quota.

Protocol reference: AssemblyAI Voice Agent WebSocket API (session.update,
input.audio, reply.audio, tool.call / tool.result, session.resume ...).
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional

import requests

from .models import Story
from .tools import TOOL_DEFINITIONS, _outline

DEFAULT_BASE = "https://agents.assemblyai.com"


class VoiceError(Exception):
    """Human-readable voice setup failure with an HTTP status for the API layer."""

    def __init__(self, message: str, status: int = 502, code: str = "voice_error") -> None:
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def base_url() -> str:
    return os.environ.get("ASSEMBLYAI_BASE_URL", DEFAULT_BASE).rstrip("/")


def ws_url() -> str:
    b = base_url()
    return ("wss://" + b[len("https://"):] if b.startswith("https://") else "ws://" + b[len("http://"):]) + "/v1/ws"


def is_configured() -> bool:
    return bool(os.environ.get("ASSEMBLYAI_API_KEY", "").strip())


def mint_token() -> str:
    key = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
    if not key:
        raise VoiceError(
            "Voice is not set up on this server yet (missing ASSEMBLYAI_API_KEY). "
            "You can still try the scripted demo.", 503, "missing_key")
    params = {
        "expires_in_seconds": int(os.environ.get("VOICE_TOKEN_TTL", "300")),
        "max_session_duration_seconds": int(os.environ.get("VOICE_MAX_SESSION_SECONDS", "1800")),
    }
    try:
        r = requests.get(f"{base_url()}/v1/token", params=params,
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
    except requests.RequestException:
        raise VoiceError("I couldn't reach the voice service. Check your connection and try again.", 502, "unreachable") from None
    if r.status_code in (401, 403):
        raise VoiceError("The voice service rejected this server's API key. The site owner needs to fix it.", 502, "bad_key")
    if r.status_code == 429:
        raise VoiceError("The voice service is busy right now. Please try again in a moment.", 429, "rate_limited")
    if not r.ok:
        raise VoiceError("The voice service is having trouble. Please try again shortly.", 502, "upstream_error")
    token = (r.json() or {}).get("token")
    if not token:
        raise VoiceError("The voice service returned no session token.", 502, "no_token")
    return token


# ---------------------------------------------------------------------------
# rate limiting (per client, in memory)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


token_limiter = RateLimiter(int(os.environ.get("VOICE_SESSIONS_PER_HOUR", "30")), 3600.0)


# ---------------------------------------------------------------------------
# prompt + session config
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are NarraFlow, a warm, quick co-storyteller. The user tells a story out loud; you turn it into illustrated scenes on their screen while chatting with them. You speak aloud, so keep every reply SHORT (one sentence, two at most). Never use lists, markdown, ids or the word "tool".

TWO KINDS OF TURNS - decide first, every turn:
1. NARRATION: the user is telling the story ("Once upon a time...", "She walked into the forest and found a tree that glowed..."). Storytelling is usually third person, past tense, about new events.
2. INSTRUCTION: the user is talking TO you about the story: "make the forest snowy", "actually, change her dress to blue", "regenerate scene two", "add a dragon to the final scene", "make the ending happier", "play the story", "what do we have so far?", "continue".
When unsure, treat words aimed at you or about changing something that already exists as an instruction.

NARRATION -> call add_story_content, then say one tiny line ("Lovely, painting that now.") and stop. Do not retell the story back.
- Split what was said into scenes: one scene per distinct visual moment, usually 1-3 per turn, not one per sentence.
- "narration" holds the storyteller's own words, lightly cleaned of fillers. NEVER invent plot.
- Give every character concrete visuals (age, skin, hair, outfit with colours). If the user did not say, choose fitting details, and mention them once ("I'm picturing Amara with long braids and a blue dress, tell me if you'd like her different"). Send only NEW or CHANGED characters and locations; the system remembers earlier ones and reuses the exact same look in every scene.
- If the user pauses mid-thought ("and then she..." / "the tree was..."), do NOT call anything. Say only "Mm-hm." or "Go on." and wait.

INSTRUCTIONS:
- change a character's look ("make Amara younger", "her dress is blue now") -> modify_character (send the FULL updated appearance/clothing field). Every scene with them repaints.
- change the whole story's look or mood ("more mysterious", "watercolour") -> modify_story_style.
- change one scene ("regenerate scene two", "make the tree blue", "add a dragon to the last scene", "make the ending happier") -> regenerate_scene with a clear revision_instruction. Scene numbers are 1-based; "the last/final scene" is the highest number. If you are unsure which scene or how many exist, call preview_story first. If they name a scene but not the change, ask what to change.
- add or move scenes -> add_scene / reorder_scene. Deleting -> ask "Delete scene N?" and only after a clear yes call remove_scene with confirmed=true.
- "show me / play the story" -> play_story. "export / make the video" -> export_story. "what have we got?" -> preview_story, then summarise briefly.
- "continue": say "I'm listening - what happens next?". Only if they ask for ideas, offer ONE short suggestion and add it only after they say yes.
- Confirm big changes in a few words, e.g. "Done, repainting every scene with her in it."

WHILE YOU WORK: illustrations take a few seconds and appear on screen by themselves. Say you are painting them; never claim they are finished until told a scene is ready. If a tool result says ok=false, tell the user the reason in plain words and what to do next. You are NarraFlow - do not describe how you work internally. Speak the user's language (default English)."""


def build_greeting(story: Optional[Story]) -> str:
    if story and story.scenes:
        return f"Welcome back to {story.title}. Shall we pick up where we left off?"
    return "Hi, I'm NarraFlow. Tell me a story and I'll paint it as you go. What shall we begin with?"


def build_system_prompt(story: Optional[Story]) -> str:
    if not story or not (story.scenes or story.characters):
        return SYSTEM_PROMPT
    import json

    return f"{SYSTEM_PROMPT}\n\nCURRENT STORY (already on screen, do not re-add it):\n{json.dumps(_outline(story))}"


def build_session(story: Optional[Story]) -> Dict[str, Any]:
    """The `session.update` `session` object for an inline agent."""
    mode = os.environ.get("VOICE_TOOL_EXECUTION_MODE", "interactive")
    tools = [{**t, "execution_mode": mode} for t in TOOL_DEFINITIONS]
    output: Dict[str, Any] = {}
    if os.environ.get("ASSEMBLYAI_VOICE"):
        output["voice"] = os.environ["ASSEMBLYAI_VOICE"]
    session: Dict[str, Any] = {
        "system_prompt": build_system_prompt(story),
        "greeting": build_greeting(story),
        "input": {
            # Storytellers pause to think. Wait longer than a chat assistant before
            # deciding the turn is over, but never longer than max_silence.
            "turn_detection": {
                "vad_threshold": float(os.environ.get("VOICE_VAD_THRESHOLD", "0.5")),
                "min_silence": int(os.environ.get("VOICE_MIN_SILENCE_MS", "1300")),
                "max_silence": int(os.environ.get("VOICE_MAX_SILENCE_MS", "4500")),
                "interrupt_response": True,
            },
        },
        "tools": tools,
    }
    if output:
        session["output"] = output
    names = [c.name for c in story.characters] if story else []
    if names:
        session["input"]["keyterms"] = names[:50]  # help recognition of invented names
    return session
