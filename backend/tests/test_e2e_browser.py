"""End-to-end tests in a real (headless) Chrome with a fake microphone.

The AssemblyAI service is replaced by tests/fake_assemblyai.py, which speaks the
documented Voice Agent protocol. These prove the browser pipeline (mic capture ->
PCM16/base64 -> WebSocket -> tool calls -> story engine -> UI) works; they do not
prove behaviour against the real service.
"""
import base64
import json
import shutil
import time

import httpx
import pytest

from app.studio import agent

pw = pytest.importorskip("playwright.sync_api")
CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium installed")

from .fake_assemblyai import API_KEY, FakeAssemblyAI  # noqa: E402

FLAGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"]


@pytest.fixture(scope="module")
def playwright_ctx():
    with pw.sync_playwright() as p:
        yield p


@pytest.fixture()
def browser(playwright_ctx):
    b = playwright_ctx.chromium.launch(executable_path=CHROME, headless=True, args=FLAGS)
    yield b
    b.close()


@pytest.fixture()
def fake():
    f = FakeAssemblyAI()
    f.start()
    yield f
    f.stop()


@pytest.fixture()
def voice_app(fake, live_server, monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", API_KEY)
    monkeypatch.setenv("ASSEMBLYAI_BASE_URL", f"http://127.0.0.1:{fake.port}")
    monkeypatch.setattr(agent, "token_limiter", agent.RateLimiter(100, 3600))
    return live_server


def open_studio(browser, url, viewport=None):
    ctx = browser.new_context(permissions=["microphone"], viewport=viewport or {"width": 1400, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None)
    page.goto(url + "/app/studio.html")
    page.wait_for_selector("#mic")
    return page, errors


def wait_for(cond, timeout=20, step=0.1, msg="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {msg}")


def story(base, sid):
    return httpx.get(f"{base}/api/stories/{sid}").json()


def test_live_voice_session_drives_the_story_engine(browser, voice_app, fake):
    page, errors = open_studio(browser, voice_app)
    page.click("#mic")

    # connection + session.update
    wait_for(lambda: fake.messages("session.update"), msg="session.update")
    cfg = fake.messages("session.update")[0]["session"]
    assert len(cfg["tools"]) == 13 and "NarraFlow" in cfg["system_prompt"] and cfg["input"]["turn_detection"]["interrupt_response"]
    assert API_KEY not in json.dumps(fake.log), "the permanent key must never be sent by the browser"
    assert len(fake.tokens) == 1 and fake.http_log[0][1]["authorization"] == f"Bearer {API_KEY}"  # server-side only
    wait_for(lambda: page.locator("#mic").get_attribute("aria-pressed") == "true", msg="mic pressed state")

    # microphone audio: PCM16 @ 24 kHz, ~50 ms chunks, real signal from the fake device
    # (Chrome's fake device beeps about once a second, so look at ~3 s of audio, not the first few chunks.)
    wait_for(lambda: len(fake.messages("input.audio")) >= 70, timeout=30, msg="mic audio")
    chunks = [base64.b64decode(m["audio"]) for m in fake.messages("input.audio")[:70]]
    assert all(2000 <= len(c) <= 3200 and len(c) % 2 == 0 for c in chunks), [len(c) for c in chunks]
    loud = [max(abs(int.from_bytes(c[i:i + 2], "little", signed=True)) for i in range(0, len(c), 2)) for c in chunks]
    assert max(loud) > 500, f"the microphone signal never reached the wire (peak {max(loud)})"

    # the tool call runs against the real story engine and the UI follows
    sid = wait_for(lambda: page.evaluate("localStorage.getItem('narraflow.story')"), msg="active story id")
    wait_for(lambda: story(voice_app, sid)["scenes"] and story(voice_app, sid)["scenes"][0]["status"] == "ready", msg="scene painted")
    page.wait_for_selector("#sceneList .scard")
    assert "Amara" in page.locator("#charList").inner_text()
    assert page.locator("#empty").is_hidden()
    page.wait_for_selector("#sceneImg", state="visible", timeout=10000)  # revealed once the live stream reports it ready
    assert page.locator("#sceneImg").get_attribute("src").startswith("/api/assets/")

    # tool result is returned to the agent as a JSON string, and the agent's follow-up is shown
    wait_for(lambda: getattr(fake, "tool_results", None), msg="tool.result")
    res = fake.tool_results[0]
    assert res["call_id"] == "call_1" and isinstance(res["result"], str) and json.loads(res["result"])["ok"] is True
    wait_for(lambda: "Tell me what happens next" in page.locator("#liveTxt").inner_text(), msg="agent reply shown live")
    log = page.locator("#log").text_content()  # (panel is collapsed by default, so read the DOM text)
    assert "Once upon a time" in log and "Lovely, painting that now." in log and "Tell me what happens next." in log

    # ending the conversation sends session.end (avoids the billable resume window)
    page.click("#mic")
    wait_for(lambda: fake.messages("session.end"), msg="session.end")
    assert page.locator("#mic").get_attribute("aria-pressed") == "false"
    assert not errors, errors


def test_barge_in_cuts_agent_audio_immediately(browser, voice_app, fake):
    fake.mode = "long_reply"
    page, errors = open_studio(browser, voice_app)
    page.click("#mic")
    wait_for(lambda: page.locator("#mic").get_attribute("data-agent") == "speaking", timeout=25, msg="agent speaking")
    fake.push({"type": "input.speech.started"})  # the user talks over the agent
    wait_for(lambda: page.locator("#mic").get_attribute("data-agent") in ("", None), timeout=3, msg="agent audio flushed")
    assert page.locator("#statusLabel").inner_text().startswith("Listening")
    fake.push({"type": "reply.done", "status": "interrupted"})
    assert not errors, errors


def test_dropped_connection_resumes_with_a_fresh_token(browser, voice_app, fake):
    fake.mode = "silent"
    page, errors = open_studio(browser, voice_app)
    page.click("#mic")
    wait_for(lambda: fake.messages("session.update"), msg="first session")
    sid = wait_for(lambda: list(fake.sessions), msg="session id")[0]
    wait_for(lambda: len(fake.messages("input.audio")) >= 3, msg="audio flowing")
    fake.drop_connections()
    resume = wait_for(lambda: fake.messages("session.resume"), timeout=10, msg="session.resume")
    assert resume[0]["session_id"] == sid
    assert len(fake.used_tokens) == 2, "single-use tokens: the reconnect must fetch a new one"
    before = len(fake.messages("input.audio"))
    wait_for(lambda: len(fake.messages("input.audio")) > before + 3, timeout=10, msg="audio resumed after reconnect")
    assert page.locator("#statusLabel").inner_text().startswith("Listening")
    assert not [e for e in errors if "WebSocket" not in e and "net::" not in e], errors


def test_missing_api_key_explains_and_offers_the_demo(browser, live_server, monkeypatch):
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    page, errors = open_studio(browser, live_server)
    page.click("#mic")
    wait_for(lambda: "not set up" in page.locator("#bannerText").inner_text(), msg="setup banner")
    assert "demo" in page.locator("#bannerActions").inner_text().lower()
    assert "Traceback" not in page.content() and "TypeError" not in page.content()
    assert page.locator("#mic").get_attribute("aria-pressed") == "false"


def test_microphone_permission_denied_is_explained(playwright_ctx, voice_app, fake):
    b = playwright_ctx.chromium.launch(executable_path=CHROME, headless=True, args=["--deny-permission-prompts"])
    try:
        ctx = b.new_context(permissions=[])
        page = ctx.new_page()
        page.goto(voice_app + "/app/studio.html")
        page.wait_for_selector("#mic")
        page.click("#mic")
        wait_for(lambda: page.locator("#banner").is_visible(), msg="permission banner")
        text = page.locator("#bannerText").inner_text().lower()
        assert "microphone" in text and ("blocked" in text or "can't find" in text)
        assert not fake.messages("session.update"), "no voice session may start without a microphone"
        assert page.locator("#mic").get_attribute("aria-pressed") == "false"
    finally:
        b.close()


def test_upstream_token_failure_is_human_readable(browser, live_server, monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "wrong-key")
    monkeypatch.setenv("ASSEMBLYAI_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setattr(agent, "token_limiter", agent.RateLimiter(100, 3600))
    page, errors = open_studio(browser, live_server)
    page.click("#mic")
    wait_for(lambda: page.locator("#banner").is_visible(), msg="error banner")
    text = page.locator("#bannerText").inner_text()
    assert "couldn't reach the voice service" in text and "Traceback" not in text and "ECONN" not in text
    assert "Try again" in page.locator("#bannerActions").inner_text()


def test_mobile_layout_has_no_horizontal_scroll_and_a_reachable_mic(browser, live_server):
    page, _ = open_studio(browser, live_server, viewport={"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), "horizontal overflow"
    box = page.locator("#mic").bounding_box()
    assert box and box["width"] >= 44 and box["height"] >= 44, "touch target too small"
    page.locator("#mic").scroll_into_view_if_needed()
    assert page.locator("#mic").is_visible()


def test_scripted_demo_runs_to_playback_and_exports(browser, live_server):
    page, errors = open_studio(browser, live_server)
    page.click("#btnDemo")
    page.wait_for_selector("#playerBar:not([hidden])", timeout=120000)
    assert page.locator("#sceneList .scard").count() == 5
    assert page.locator("#charList .char").count() == 2
    assert page.locator("#banner.banner:not(.info)").count() == 0, page.locator("#bannerText").inner_text()
    assert page.locator("#sceneImg.kb").count() == 1, "Ken Burns motion on the playing scene"
    wait_for(lambda: page.locator("#pTime").inner_text() not in ("0:00", ""), timeout=10, msg="playback progressing")
    assert "0:" in page.locator("#pTime").inner_text() and "/" in page.locator("#pTime").inner_text()
    assert page.locator("#cap").is_visible() and len(page.locator("#cap").inner_text()) > 10  # captions
    page.click("#pClose")
    page.click("#btnExport")
    page.wait_for_selector("#btnDownload", state="visible", timeout=120000)  # a visible button, not buried in a panel
    href = page.locator("#btnDownload").get_attribute("href")
    r = httpx.get(live_server + href)
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and len(r.content) > 20000
    assert not errors, errors


def test_original_pipeline_page_still_loads_cleanly(browser, live_server):
    ctx = browser.new_context(viewport={"width": 1300, "height": 800})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(live_server + "/app/classic.html")
    page.wait_for_timeout(1000)
    assert "Multi-Agent Storytelling" in page.title()
    assert "voice studio" in page.locator(".tag").first.inner_text().lower()
    assert not errors, errors
    page.goto(live_server + "/app/")  # the new landing page is the default entry point
    assert "illustrated story" in page.locator("h1").inner_text().lower()
