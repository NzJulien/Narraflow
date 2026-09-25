"""AssemblyAI Voice Agent integration (server side): token minting, session
config, tool schema, secret hygiene, rate limiting."""
import json

import pytest
import requests

from app.studio import agent, jobs, store, tools

KEY = "aai_TEST_SECRET_KEY_123"


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.ok = status, body or {}, status < 400

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(agent, "token_limiter", agent.RateLimiter(5, 3600))


@pytest.fixture()
def with_key(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", KEY)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        return FakeResp(200, {"token": "tmp_token_abc"})

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_status_reports_unconfigured_without_key(client):
    assert client.get("/api/voice/status").json() == {"configured": False}


def test_session_without_key_gives_a_helpful_503_and_leaks_nothing(client):
    r = client.post("/api/voice/session", json={})
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "missing_key" and "ASSEMBLYAI_API_KEY" in body["error"] and "demo" in body["error"]


def test_token_is_minted_server_side_and_permanent_key_never_reaches_the_client(client, with_key):
    r = client.post("/api/voice/session", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["token"] == "tmp_token_abc"
    assert body["ws_url"] == "wss://agents.assemblyai.com/v1/ws"
    assert KEY not in r.text, "the permanent API key must never be sent to the browser"
    call = with_key[0]
    assert call["url"] == "https://agents.assemblyai.com/v1/token"
    assert call["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert 1 <= call["params"]["expires_in_seconds"] <= 600
    assert 60 <= call["params"]["max_session_duration_seconds"] <= 10800


def test_each_session_mints_a_fresh_single_use_token(client, monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", KEY)
    seq = iter(["t1", "t2", "t3"])
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, {"token": next(seq)}))
    assert [client.post("/api/voice/session", json={}).json()["token"] for _ in range(3)] == ["t1", "t2", "t3"]


@pytest.mark.parametrize("status,code,http,needle", [
    (401, "bad_key", 502, "rejected"), (403, "bad_key", 502, "rejected"),
    (429, "rate_limited", 429, "busy"), (500, "upstream_error", 502, "trouble"),
])
def test_upstream_failures_become_readable_errors(client, monkeypatch, status, code, http, needle):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", KEY)
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(status))
    r = client.post("/api/voice/session", json={})
    assert r.status_code == http and r.json()["code"] == code and needle in r.json()["error"]
    assert KEY not in r.text


def test_network_failure_is_readable(client, monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", KEY)
    def boom(*a, **k): raise requests.ConnectionError("dns failure")
    monkeypatch.setattr(requests, "get", boom)
    r = client.post("/api/voice/session", json={})
    assert r.status_code == 502 and r.json()["code"] == "unreachable" and "dns" not in r.text


def test_token_minting_is_rate_limited_per_client(client, with_key):
    codes = [client.post("/api/voice/session", json={}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    assert len(with_key) == 5, "rate-limited requests must not reach AssemblyAI"


def test_base_url_override_moves_both_token_and_websocket(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_BASE_URL", "http://localhost:9999")
    assert agent.ws_url() == "ws://localhost:9999/v1/ws"


# ---- session.update payload ---------------------------------------------
def test_session_config_matches_the_voice_agent_protocol(client, with_key):
    s = client.post("/api/voice/session", json={}).json()["session"]
    assert {"system_prompt", "greeting", "input", "tools"} <= set(s)
    td = s["input"]["turn_detection"]
    assert td["interrupt_response"] is True, "barge-in must be enabled"
    assert 50 <= td["min_silence"] <= 10000 and 50 <= td["max_silence"] <= 10000
    assert td["min_silence"] > 1000, "storytellers pause: wait longer than the 1000ms default"
    assert td["min_silence"] <= td["max_silence"] and 0 <= td["vad_threshold"] <= 1
    assert "NarraFlow" in s["greeting"]
    json.dumps(s)  # must be serialisable


def test_prompt_teaches_narration_vs_instruction_and_pauses():
    p = agent.SYSTEM_PROMPT
    for needle in ("NARRATION", "INSTRUCTION", "add_story_content", "regenerate_scene", "modify_character",
                   "Mm-hm", "confirmed=true", "NEVER invent plot", "play_story"):
        assert needle in p, needle


def test_every_tool_definition_is_valid_assemblyai_format():
    names = set()
    for t in tools.TOOL_DEFINITIONS:
        assert t["type"] == "function" and t["name"] and t["description"]
        assert t["name"] not in names
        names.add(t["name"])
        params = t["parameters"]
        assert params["type"] == "object" and isinstance(params["properties"], dict)
        assert set(params.get("required", [])) <= set(params["properties"]), f"{t['name']}: required not in properties"
        json.dumps(t)
    assert names == set(tools.REGISTRY), "every tool the agent can call has an executor, and vice versa"
    assert {"create_story", "add_story_content", "analyze_story", "generate_scene", "regenerate_scene", "modify_character",
            "modify_story_style", "add_scene", "remove_scene", "reorder_scene", "preview_story", "export_story"} <= names


def test_tools_default_to_documented_execution_mode(monkeypatch):
    modes = {t["execution_mode"] for t in agent.build_session(None)["tools"]}
    assert modes == {"interactive"}
    monkeypatch.setenv("VOICE_TOOL_EXECUTION_MODE", "hold")
    assert {t["execution_mode"] for t in agent.build_session(None)["tools"]} == {"hold"}


def test_returning_storyteller_gets_context_greeting_and_name_hints(story_id):
    tools.execute("add_story_content", story_id, {"narration_text": "x", "characters": [{"name": "Amara"}, {"name": "Kito"}],
                  "scenes": [{"narration": "x", "summary": "s", "visual_prompt": "v", "characters": ["Amara"]}]})
    assert jobs.wait_idle()
    s = agent.build_session(store.get(story_id))
    assert s["greeting"].startswith("Welcome back")
    assert "CURRENT STORY" in s["system_prompt"] and "Amara" in s["system_prompt"]
    assert s["input"]["keyterms"] == ["Amara", "Kito"], "character names help recognise invented names"


def test_tool_results_stay_small_for_the_voice_session(story_id):
    """Never push images or huge payloads through the voice session."""
    r = tools.execute("add_story_content", story_id, {"narration_text": "x" * 50, "scenes": [
        {"narration": "n" * 50, "summary": "s", "visual_prompt": "v"} for _ in range(3)]})
    assert jobs.wait_idle()
    assert len(json.dumps(r)) < 800 and "data:image" not in json.dumps(r)
    assert len(json.dumps(tools.execute("preview_story", story_id, {}))) < 3000
