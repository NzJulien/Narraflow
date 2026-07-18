"""
Shared agent utilities
----------------------
Small helpers that every agent module (Director, Writer, Memory, Artist,
Cinematographer, Voice) needs, factored out so the same boilerplate
isn't copy-pasted five times and can't drift out of sync:

  - Fireworks config (base URL + API key) read from the environment once.
  - resolve_backend(): the "if the remote backend was requested but its
    prerequisites aren't met, log and fall back" dance each agent did by
    hand, with per-agent log prefixes and messages.
  - make_logger(): a "[agent] message" print helper.
  - seed_from(): the deterministic sha1 seed used by the mock backends so
    the same inputs always produce the same "random" choices.
  - auth_headers(): the Fireworks/vLLM request headers.

All of this is behavior-preserving: seeds, log lines, and request
headers are identical to what the individual modules produced before.
"""

import hashlib
import os
from typing import Optional, Tuple

FIREWORKS_BASE_URL = os.environ.get(
    "FIREWORKS_BASE_URL", "https://api.fireworks.ai/inference/v1"
)


def get_api_key() -> str:
    return os.environ.get("FIREWORKS_API_KEY", "").strip()


def make_logger(prefix: str):
    """Returns a `_log(msg)` that prints `[prefix] msg`."""

    def _log(msg: str) -> None:
        print(f"[{prefix}] {msg}")

    return _log


def resolve_backend(
    prefix: str,
    requested: str,
    *,
    remote: str,
    fallback: str,
    missing_msg: str,
    ready_msg: Optional[str] = None,
    extra_ready: bool = True,
) -> Tuple[str, Optional[str]]:
    """
    Decide which backend an agent actually runs on.

    If `requested` is the remote backend, require a Fireworks API key
    (and any extra prerequisite via `extra_ready`); when either is
    missing, log `missing_msg` and drop to `fallback`. Otherwise keep the
    requested backend. Returns (backend, api_key) - api_key is None
    whenever the remote backend isn't active.
    """
    if requested != remote:
        return requested, None

    api_key = get_api_key()
    if not api_key or not extra_ready:
        make_logger(prefix)(missing_msg)
        return fallback, None

    if ready_msg:
        make_logger(prefix)(ready_msg)
    return remote, api_key


def seed_from(*parts: object) -> int:
    """Deterministic integer seed from the given parts (joined with ':')."""
    raw = ":".join(str(p) for p in parts)
    return int(hashlib.sha1(raw.encode()).hexdigest(), 16)


def auth_headers(api_key: Optional[str] = None, *, content_type: bool = True) -> dict:
    """
    Build request headers for a Fireworks/vLLM call: a Bearer
    Authorization header when `api_key` is set (Fireworks) and omitted
    when it isn't (self-hosted vLLM), plus a JSON Content-Type unless
    `content_type=False` (e.g. multipart audio uploads).
    """
    headers: dict = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers
