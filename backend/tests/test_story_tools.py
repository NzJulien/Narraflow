"""Story engine: structured state, continuity, voice-driven edits, jobs."""
import struct
import threading

import pytest

from app.studio import images, jobs, store, tools
from app.studio.images import ImageError, StorybookIllustrator
from app.studio.prompts import compose_scene_prompt, derive_traits

AMARA = {"name": "Amara", "description": "a curious young girl", "age": "about 9",
         "appearance": "dark skin, long black braids", "clothing": "blue dress, brown sandals, yellow bracelet"}


def add(story_id, scenes, characters=None, **extra):
    return tools.execute("add_story_content", story_id, {
        "narration_text": " ".join(s["narration"] for s in scenes), "characters": characters or [], "scenes": scenes, **extra})


def scene(text, chars=("Amara",), setting="the village", visual=""):
    return {"narration": text, "summary": text[:50], "setting": setting, "characters": list(chars), "visual_prompt": visual or text}


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", head[16:24])


# ---- structure -----------------------------------------------------------
def test_narration_becomes_structured_story(story_id):
    r = add(story_id, [scene("Amara lived in a village."), scene("She saw a glow.")], [AMARA])
    assert r["ok"] and r["added_scenes"] == [1, 2] and r["characters"] == ["Amara"]
    s = store.get(story_id)
    assert [x.order for x in s.scenes] == [1, 2]
    amara = s.character("amara")  # case-insensitive lookup
    assert amara.persistent_visual_traits[:3] == ["about 9", "dark skin", "long black braids"]
    assert s.location("the village") is not None  # auto-registered from the scene
    assert "Amara lived" in s.narration and "She saw a glow" in s.narration


def test_prose_without_scene_split_is_not_lost(story_id):
    r = tools.execute("add_story_content", story_id, {"narration_text": "A fox crossed the frozen lake."})
    assert r["ok"] and r["added_scenes"] == [1]
    assert "fox" in store.get(story_id).scenes[0].narration


def test_empty_content_is_refused(story_id):
    r = tools.execute("add_story_content", story_id, {})
    assert r["ok"] is False and "didn't catch" in r["error"]


def test_lenient_argument_types(story_id):
    tools.execute("add_story_content", story_id, {
        "narration_text": "x", "characters": [AMARA],
        "scenes": [{"narration": "Hello there friend", "summary": "s", "visual_prompt": "v", "characters": "Amara, Kito"}]})
    s = store.get(story_id)
    assert s.scenes[0].characters == ["Amara", "Kito"]  # comma string coerced, unknown Kito auto-created
    assert s.character("Kito") is not None
    r = tools.execute("regenerate_scene", story_id, {"scene_number": "1", "revision_instruction": "snowy"})
    assert r["ok"]  # "1" as a string works


def test_create_story_updates_blank_story_in_place_then_forks(story_id):
    r = tools.execute("create_story", story_id, {"title": "The Lantern Tree", "tone": "wondrous"})
    assert r["story_id"] == story_id and r["new_story"] is False
    add(story_id, [scene("one")], [AMARA])
    r2 = tools.execute("create_story", story_id, {"title": "Another"})
    assert r2["new_story"] is True and r2["story_id"] != story_id
    assert store.get(story_id).title == "The Lantern Tree"  # original untouched


# ---- continuity ----------------------------------------------------------
def test_character_traits_are_injected_into_every_scene_prompt(story_id):
    add(story_id, [scene("a", visual="dusk village"), scene("b", visual="forest at night", setting="the forest")], [AMARA])
    s = store.get(story_id)
    for sc in s.scenes:
        p = compose_scene_prompt(s, sc)
        for trait in ("dark skin", "long black braids", "blue dress", "yellow bracelet"):
            assert trait in p, f"{trait!r} missing from scene {sc.order} prompt"


def test_modify_character_replaces_old_traits_and_repaints_only_their_scenes(story_id):
    add(story_id, [scene("Amara walks", chars=("Amara",)), scene("An empty hall", chars=(), setting="the hall"),
                   scene("Amara sits", chars=("Amara",))], [AMARA])
    assert jobs.wait_idle()
    before = {x.order: x.image_url for x in store.get(story_id).scenes}
    assert all(before.values())

    r = tools.execute("modify_character", story_id, {"character_name": "amara", "changes": {"clothing": "green dress, brown sandals"}})
    assert r["ok"] and r["scenes_being_repainted"] == 2
    assert jobs.wait_idle()
    s = store.get(story_id)
    traits = s.character("Amara").persistent_visual_traits
    assert "green dress" in traits and "blue dress" not in traits and "yellow bracelet" not in traits
    after = {x.order: x.image_url for x in s.scenes}
    assert after[1] != before[1] and after[3] != before[3], "scenes with Amara must be repainted"
    assert after[2] == before[2], "a scene without Amara must not be regenerated"
    assert "green dress" in compose_scene_prompt(s, s.scene_by_number(1))


def test_modify_unknown_character_is_a_speakable_refusal(story_id):
    add(story_id, [scene("x")], [AMARA])
    r = tools.execute("modify_character", story_id, {"character_name": "Zed", "changes": {"age": "old"}})
    assert r["ok"] is False and "Amara" in r["error"]


def test_rename_character_updates_scenes(story_id):
    add(story_id, [scene("x")], [AMARA])
    tools.execute("modify_character", story_id, {"character_name": "Amara", "changes": {"name": "Nia"}})
    s = store.get(story_id)
    assert s.scenes[0].characters == ["Nia"] and s.character("Nia")


def test_style_change_repaints_everything_and_is_persistent(story_id):
    add(story_id, [scene("a"), scene("b")], [AMARA])
    assert jobs.wait_idle()
    r = tools.execute("modify_story_style", story_id, {"style_changes": "misty, mysterious lighting"})
    assert r["scenes_being_repainted"] == 2
    assert jobs.wait_idle()
    s = store.get(story_id)
    assert "mysterious" in s.visual_style and "mysterious" in compose_scene_prompt(s, s.scenes[0])


# ---- scene edits ---------------------------------------------------------
def test_regenerate_scene_tracks_revision_and_changes_image(story_id):
    add(story_id, [scene("The tree", chars=(), visual="a tree in a clearing")])
    assert jobs.wait_idle()
    first = store.get(story_id).scenes[0].image_url
    r = tools.execute("regenerate_scene", story_id, {"scene_number": 1, "revision_instruction": "make the tree blue"})
    assert r["ok"] and r["revision"] == 1
    assert jobs.wait_idle()
    sc = store.get(story_id).scenes[0]
    assert sc.status == "ready" and sc.image_url != first and "blue" in sc.revision_note


def test_regenerate_requires_instruction_and_valid_scene(story_id):
    add(story_id, [scene("x")], [AMARA])
    assert "what should change" in tools.execute("regenerate_scene", story_id, {"scene_number": 1})["error"].lower()
    r = tools.execute("regenerate_scene", story_id, {"scene_number": 9, "revision_instruction": "x"})
    assert r["ok"] is False and "no scene 9" in r["error"] and "1 scene" in r["error"]


def test_generate_scene_skips_unchanged_ready_scene(story_id):
    add(story_id, [scene("x")], [AMARA])
    assert jobs.wait_idle()
    r = tools.execute("generate_scene", story_id, {"scene_number": 1})
    assert "already illustrated" in r["message"]
    r = tools.execute("generate_scene", story_id, {"scene_number": 1, "force": True})
    assert "Painting" in r["message"]
    assert jobs.wait_idle()


def test_remove_scene_needs_explicit_confirmation(story_id):
    add(story_id, [scene("one"), scene("two"), scene("three")], [AMARA])
    r = tools.execute("remove_scene", story_id, {"scene_number": 2})
    assert r["ok"] is False and r["needs_confirmation"] and len(store.get(story_id).scenes) == 3
    r = tools.execute("remove_scene", story_id, {"scene_number": 2, "confirmed": True})
    s = store.get(story_id)
    assert r["ok"] and [x.narration for x in s.scenes] == ["one", "three"] and [x.order for x in s.scenes] == [1, 2]


def test_reorder_and_insert_keep_numbering_contiguous(story_id):
    add(story_id, [scene("A"), scene("B"), scene("C")], [AMARA])
    tools.execute("reorder_scene", story_id, {"scene_number": 3, "new_position": 1})
    assert [x.narration for x in store.get(story_id).scenes] == ["C", "A", "B"]
    tools.execute("add_scene", story_id, {"narration": "NEW", "summary": "n", "position": 2})
    s = store.get(story_id)
    assert [x.narration for x in s.scenes] == ["C", "NEW", "A", "B"] and [x.order for x in s.scenes] == [1, 2, 3, 4]
    assert jobs.wait_idle()


def test_preview_and_play(story_id):
    assert tools.execute("play_story", story_id, {})["ok"] is False  # nothing to play yet
    add(story_id, [scene("x")], [AMARA])
    p = tools.execute("preview_story", story_id, {})
    assert p["ok"] and p["outline"]["scenes"][0]["scene_number"] == 1
    assert jobs.wait_idle()
    pl = tools.execute("play_story", story_id, {})
    assert pl["ok"] and pl["ui_action"] == "play" and pl["ready_scenes"] == 1


def test_unknown_tool_and_bad_json_never_raise(story_id):
    assert tools.execute("make_coffee", story_id, {})["ok"] is False
    assert tools.execute("preview_story", story_id, "{not json")["ok"] is False


# ---- images & jobs -------------------------------------------------------
def test_generated_image_is_a_real_16_9_png(story_id):
    add(story_id, [scene("Amara under a glowing tree", visual="glowing tree")], [AMARA])
    assert jobs.wait_idle()
    sc = store.get(story_id).scenes[0]
    assert sc.status == "ready"
    path = jobs.assets_dir(story_id) + "/" + sc.image_url.rsplit("/", 1)[1]
    assert png_size(path) == (1280, 720)


def test_illustrator_reflects_character_colours_and_edits():
    from app.studio.models import Character

    ill = StorybookIllustrator()
    def dominant_dress(clothing):
        c = Character(name="A", description="young girl", clothing=clothing, persistent_visual_traits=[clothing])
        raw = ill.generate_character_reference(images.SceneRequest(prompt="p", characters=[c], seed=1))
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        return im.getpixel((360, 480))
    r, g, b = dominant_dress("red dress")
    assert r > b and r > g
    r, g, b = dominant_dress("blue dress")
    assert b > r and b > g


class FailingProvider(images.ImageGenerationProvider):
    name = "failing"
    def generate_scene(self, req):
        raise ImageError("The illustration service is rate limited right now. Try again shortly.")


def test_provider_failure_is_isolated_readable_and_retryable(story_id, monkeypatch):
    real = images.get_provider()
    monkeypatch.setattr(images, "_provider", FailingProvider())
    add(story_id, [scene("one"), scene("two")], [AMARA])
    assert jobs.wait_idle()
    s = store.get(story_id)
    assert [x.status for x in s.scenes] == ["failed", "failed"]
    assert "rate limited" in s.scenes[0].error and "Traceback" not in s.scenes[0].error
    assert len(s.scenes) == 2 and s.narration  # story content survives an image failure
    monkeypatch.setattr(images, "_provider", real)  # provider recovers
    tools.execute("generate_scene", story_id, {"scene_number": 1})
    assert jobs.wait_idle()
    assert store.get(story_id).scene_by_number(1).status == "ready"


def test_unexpected_provider_crash_hides_internals(story_id, monkeypatch):
    class Boom(images.ImageGenerationProvider):
        def generate_scene(self, req):
            raise ValueError("secret internal path /srv/x")
    real = images.get_provider()
    monkeypatch.setattr(images, "_provider", Boom())
    add(story_id, [scene("one")], [AMARA])
    assert jobs.wait_idle()
    sc = store.get(story_id).scenes[0]
    assert sc.status == "failed" and "/srv/x" not in sc.error
    monkeypatch.setattr(images, "_provider", real)


def test_stale_render_never_overwrites_a_newer_revision(story_id, monkeypatch):
    """Edit a scene while its first image is still rendering: the slow, outdated
    result must be dropped and the newest revision must win."""
    real = images.get_provider()
    gate, started = threading.Event(), threading.Event()

    class Slow(images.ImageGenerationProvider):
        name = "slow"
        def generate_scene(self, req):
            if "OLD" in req.prompt or "revision" not in req.prompt:
                started.set()
                gate.wait(5)
            return real.generate_scene(req)
        def regenerate_scene(self, req):
            return real.generate_scene(req)

    monkeypatch.setattr(images, "_provider", Slow())
    add(story_id, [scene("The tree", chars=(), visual="a tree")])
    assert started.wait(5)
    tools.execute("regenerate_scene", story_id, {"scene_number": 1, "revision_instruction": "make it blue"})
    gate.set()
    assert jobs.wait_idle()
    sc = store.get(story_id).scenes[0]
    assert sc.status == "ready" and sc.revision == 1
    assert f"-r{sc.revision}-" in sc.image_url, "image must belong to the newest revision"
    monkeypatch.setattr(images, "_provider", real)


def test_removed_scene_during_render_does_not_crash(story_id, monkeypatch):
    real = images.get_provider()
    gate = threading.Event()

    class Slow(images.ImageGenerationProvider):
        def generate_scene(self, req):
            gate.wait(5)
            return real.generate_scene(req)

    monkeypatch.setattr(images, "_provider", Slow())
    add(story_id, [scene("one"), scene("two")], [AMARA])
    tools.execute("remove_scene", story_id, {"scene_number": 2, "confirmed": True})
    gate.set()
    assert jobs.wait_idle()
    s = store.get(story_id)
    assert len(s.scenes) == 1 and s.scenes[0].status == "ready"
    monkeypatch.setattr(images, "_provider", real)


def test_fireworks_provider_without_key_explains_itself(monkeypatch):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    with pytest.raises(ImageError) as e:
        images.FireworksFluxProvider()
    assert "FIREWORKS_API_KEY" in str(e.value)


def test_fireworks_error_mapping(monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    p = images.FireworksFluxProvider()
    import requests

    class R:
        def __init__(self, code): self.status_code, self.ok, self.content = code, code < 400, b"png"
    for code, needle in ((429, "rate limited"), (401, "rejected"), (500, "returned an error")):
        monkeypatch.setattr(requests, "post", lambda *a, _c=code, **k: R(_c))
        with pytest.raises(ImageError) as e:
            p.generate_scene(images.SceneRequest(prompt="x"))
        assert needle in str(e.value)
    def boom(*a, **k): raise requests.ConnectionError("dns")
    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(ImageError) as e:
        p.generate_scene(images.SceneRequest(prompt="x"))
    assert "could not be reached" in str(e.value)


def test_derive_traits_dedupes():
    from app.studio.models import Character
    c = Character(name="A", age="about 9", appearance="dark skin, dark skin", clothing="blue dress and brown sandals")
    assert derive_traits(c) == ["about 9", "dark skin", "blue dress", "brown sandals"]
