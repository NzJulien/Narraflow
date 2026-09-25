"""HTTP API, live event stream, asset security, export and the full demo flow."""
import json
import os
import subprocess

from app.studio import jobs, render, store


def run_demo(client):
    sid, results = None, []
    for step in client.get("/api/demo/script").json():
        r = client.post(f"/api/tools/{step['tool']}", json={"story_id": sid, "arguments": step["args"]}).json()
        sid = r.get("story_id", sid)
        results.append(r)
    assert jobs.wait_idle(60)
    return sid, results


def test_health_reports_capabilities(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["image_provider"] == "illustrator" and h["ffmpeg"] is True and h["voice_configured"] is False


def test_tool_call_without_a_story_creates_one(client):
    r = client.post("/api/tools/add_story_content", json={"arguments": {
        "narration_text": "Once upon a time", "scenes": [{"narration": "Once upon a time", "summary": "s", "visual_prompt": "v"}]}}).json()
    assert r["ok"] and r["story_id"]
    story = client.get(f"/api/stories/{r['story_id']}").json()
    assert len(story["scenes"]) == 1


def test_unknown_story_and_tool_are_clean_errors(client):
    assert client.get("/api/stories/nope").status_code == 404
    assert "doesn't exist" in client.get("/api/stories/nope").json()["detail"]
    r = client.post("/api/tools/explode", json={}).json()
    assert r["ok"] is False and "error" in r


def test_full_demo_story_runs_through_the_real_tools(client):
    sid, results = run_demo(client)
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]]
    story = client.get(f"/api/stories/{sid}").json()
    assert story["title"] == "The Lantern Tree"
    assert [s["order"] for s in story["scenes"]] == [1, 2, 3, 4, 5]
    assert all(s["status"] == "ready" and s["image_url"] for s in story["scenes"])
    assert story["scenes"][2]["revision"] == 1 and "blue" in story["scenes"][2]["revision_note"]
    assert {c["name"] for c in story["characters"]} == {"Amara", "Kito"}
    assert all(c["portrait_url"] for c in story["characters"])
    assert results[-1]["ui_action"] == "play"
    for s in story["scenes"]:  # every image is servable
        assert client.get(s["image_url"]).headers["content-type"] == "image/png"


def test_assets_are_cached_and_traversal_is_blocked(client):
    sid, _ = run_demo(client)
    url = client.get(f"/api/stories/{sid}").json()["scenes"][0]["image_url"]
    assert "immutable" in client.get(url).headers["cache-control"]
    for evil in ("/api/assets/../studio.db", "/api/assets/.._x/studio.db", f"/api/assets/{sid}/..%2f..%2fstudio.db",
                 "/api/assets/%2e%2e/studio.db", f"/api/assets/{sid}/.hidden"):
        r = client.get(evil)
        assert r.status_code in (400, 404), (evil, r.status_code)
        assert b"SQLite" not in r.content
    assert client.get(f"/api/assets/{sid}/missing.png").status_code == 404


def test_event_stream_pushes_snapshots_as_scenes_finish(live_server):
    import httpx

    sid = httpx.post(f"{live_server}/api/stories").json()["id"]
    httpx.post(f"{live_server}/api/tools/add_story_content", json={"story_id": sid, "arguments": {
        "narration_text": "x", "scenes": [{"narration": "A tree", "summary": "s", "visual_prompt": "tree"}]}})
    seen = []
    with httpx.stream("GET", f"{live_server}/api/stories/{sid}/events", timeout=30) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data:"):
                seen.append(json.loads(line[5:]))
                if seen[-1]["scenes"] and seen[-1]["scenes"][0]["status"] == "ready":
                    break
            assert len(seen) < 20
    assert seen[0]["scenes"][0]["status"] in ("generating", "ready")
    assert seen[-1]["scenes"][0]["image_url"]
    assert [s["version"] for s in seen] == sorted(s["version"] for s in seen)


def test_event_stream_for_missing_story_is_404(client):
    assert client.get("/api/stories/nope/events").status_code == 404


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,codec_name:format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    return j["streams"][0], float(j["format"]["duration"])


def test_export_renders_a_captioned_mp4_and_srt(client):
    sid, _ = run_demo(client)
    r = client.post(f"/api/stories/{sid}/export").json()
    assert r["ok"] and "Rendering" in r["message"]
    assert client.get(f"/api/stories/{sid}").json()["status"] in ("rendering", "complete")
    assert jobs.wait_idle(180)
    story = client.get(f"/api/stories/{sid}").json()
    assert story["status"] == "complete" and story["export_url"] == f"/api/stories/{sid}/export.mp4"
    dl = client.get(story["export_url"])
    assert dl.status_code == 200 and dl.headers["content-type"] == "video/mp4"
    assert "The_Lantern_Tree.mp4" in dl.headers["content-disposition"]
    path = os.path.join(render.exports_dir(), f"{sid}.mp4")
    stream, duration = probe(path)
    expected = render.TITLE_SECONDS + sum(s["duration"] for s in story["scenes"])
    assert (stream["width"], stream["height"], stream["codec_name"]) == (1280, 720, "h264")
    assert abs(duration - expected) < 1.5, (duration, expected)
    srt = client.get(f"/api/stories/{sid}/captions.srt").text
    assert srt.count("-->") == 5 and "Amara lived in a small village" in srt and "00:00:03,000 -->" in srt


def test_export_with_nothing_ready_is_refused_politely(client, story_id):
    r = client.post(f"/api/stories/{story_id}/export").json()
    assert r["ok"] is False and "nothing to export" in r["error"]
    assert client.get(f"/api/stories/{story_id}/export.mp4").status_code == 404


def test_export_failure_is_reported_not_raised(client, monkeypatch):
    sid, _ = run_demo(client)
    monkeypatch.setattr(render, "render_story", lambda s: (_ for _ in ()).throw(render.RenderError("ffmpeg is missing")))
    client.post(f"/api/stories/{sid}/export")
    assert jobs.wait_idle(30)
    s = client.get(f"/api/stories/{sid}").json()
    assert s["status"] == "draft" and s["export_error"] == "ffmpeg is missing" and s["export_url"] is None


def test_story_survives_a_restart(client):
    sid, _ = run_demo(client)
    listed = store.list_recent()
    assert listed[0]["id"] == sid and listed[0]["scenes"] == 5
    assert store.get(sid).title == "The Lantern Tree"  # read straight from SQLite


def test_original_pipeline_endpoints_still_work(client):
    assert client.get("/").json()["app"] == "NarraFlow"
    r = client.get("/story", params={"idea": "a girl finds a glowing tree", "scenes": 2}).json()
    assert len(r["scenes"]) == 2


def test_stories_cannot_be_listed_by_strangers(client):
    client.post("/api/stories")
    assert client.get("/api/stories").status_code in (404, 405), "a public list would leak other people's stories"


def test_story_ids_are_long_enough_to_be_unguessable(client):
    sid = client.post("/api/stories").json()["id"]
    assert len(sid.split("_", 1)[1]) >= 16


def test_oversized_tool_requests_are_refused(client):
    big = {"arguments": {"narration_text": "x" * 70000}}
    r = client.post("/api/tools/add_story_content", json=big)
    assert r.status_code == 413 and "too much" in r.json()["error"]


def test_malformed_tool_request_is_a_clean_400(client):
    r = client.post("/api/tools/add_story_content", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and "couldn't read" in r.json()["error"]


def test_tool_calls_are_rate_limited_per_client(client, monkeypatch):
    from app.studio import agent, routes
    monkeypatch.setattr(routes, "tool_limiter", agent.RateLimiter(3, 3600))
    codes = [client.post("/api/tools/preview_story", json={}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_story_creation_is_rate_limited(client, monkeypatch):
    from app.studio import agent, routes
    monkeypatch.setattr(routes, "story_limiter", agent.RateLimiter(2, 3600))
    assert [client.post("/api/stories").status_code for _ in range(3)] == [200, 200, 429]


def test_scene_cap_protects_image_spend(client, story_id, monkeypatch):
    from app.studio import tools
    monkeypatch.setattr(tools, "MAX_SCENES", 2)
    def mk(n):
        return {"narration": n, "summary": n, "visual_prompt": n}
    r = client.post("/api/tools/add_story_content", json={"story_id": story_id, "arguments": {"narration_text": "x", "scenes": [mk("a"), mk("b")]}}).json()
    assert r["ok"]
    r = client.post("/api/tools/add_story_content", json={"story_id": story_id, "arguments": {"narration_text": "y", "scenes": [mk("c")]}}).json()
    assert r["ok"] is False and "most I can illustrate" in r["error"]
    r = client.post("/api/tools/add_scene", json={"story_id": story_id, "arguments": mk("d")}).json()
    assert r["ok"] is False
    assert len(client.get(f"/api/stories/{story_id}").json()["scenes"]) == 2
    jobs.wait_idle()
