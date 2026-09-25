import test from "node:test";
import assert from "node:assert/strict";
import { StoryPlayer, buildTimeline, playableScenes } from "../player.js";

const scenes = [
  { id: "a", order: 1, narration: "One.", duration: 4, status: "ready", image_url: "/a.png" },
  { id: "b", order: 2, narration: "Two.", duration: 6, status: "ready", image_url: "/b.png" },
  { id: "c", order: 3, narration: "Three.", duration: 5, status: "ready", image_url: "/c.png" },
];

function rig({ speech = "instant" } = {}) {
  let t = 0, tick = null;
  const spoken = [], canceled = { n: 0 }, pending = [];
  const p = new StoryPlayer({
    now: () => t,
    setInterval: (fn) => { tick = fn; return 1; }, clearInterval: () => { tick = null; },
    speak: (text, done) => { spoken.push(text); speech === "instant" ? done() : pending.push(done); },
    cancelSpeak: () => { canceled.n++; },
  });
  p.load(scenes);
  const events = [];
  for (const ty of ["scene", "state", "end", "progress"]) p.on(ty, (d) => events.push([ty, d]));
  const advance = (sec) => { t += sec * 1000; tick && tick(); };
  return { p, advance, spoken, canceled, pending, events };
}

test("timeline accumulates scene durations", () => {
  const tl = buildTimeline(scenes);
  assert.equal(tl.total, 15);
  assert.deepEqual(tl.items.map((i) => [i.start, i.end]), [[0, 4], [4, 10], [10, 15]]);
});

test("only illustrated scenes are playable", () => {
  const story = { scenes: [...scenes, { id: "d", status: "generating", image_url: null }, { id: "e", status: "failed" }] };
  assert.deepEqual(playableScenes(story).map((s) => s.id), ["a", "b", "c"]);
});

test("plays scenes in order, speaks each narration, then ends", () => {
  const r = rig();
  assert.equal(r.p.play(0), true);
  assert.deepEqual(r.spoken, ["One."]);
  r.advance(4.1);
  assert.equal(r.p.index, 1); assert.deepEqual(r.spoken, ["One.", "Two."]);
  r.advance(6.1); r.advance(5.1);
  assert.equal(r.p.playing, false);
  assert.ok(r.events.some(([t]) => t === "end"));
  assert.deepEqual(r.events.filter(([t]) => t === "scene").map(([, d]) => d.index), [0, 1, 2]);
});

test("a scene waits for its narration to finish, but never longer than the grace period", () => {
  const r = rig({ speech: "manual" });
  r.p.play(0);
  r.advance(5);                       // past duration, narration still going
  assert.equal(r.p.index, 0);
  r.pending[0]();                     // narration ends
  r.advance(0.2);
  assert.equal(r.p.index, 1);
  r.advance(6.1);                     // narration never ends (speech engine hung)
  assert.equal(r.p.index, 1);
  r.advance(6.1);
  assert.equal(r.p.index, 2, "grace period elapsed");
});

test("pause freezes time, resume continues from the same point", () => {
  const r = rig();
  r.p.play(0);
  r.advance(2);
  r.p.pause();
  const frozen = r.p.elapsed();
  r.advance(50);
  assert.equal(r.p.elapsed(), frozen);
  assert.equal(r.p.index, 0);
  r.p.resume();
  r.advance(1.9);
  assert.equal(r.p.index, 0, "only ~1.9s of the 4s scene has passed");
  r.advance(0.3);
  assert.equal(r.p.index, 1);
});

test("next / previous / replay / seek", () => {
  const r = rig();
  r.p.play(0);
  r.p.next(); assert.equal(r.p.index, 1);
  r.p.next(); r.p.next(); assert.equal(r.p.index, 2, "clamped at the last scene");
  r.p.prev(); assert.equal(r.p.index, 1);
  r.p.seek(0); assert.equal(r.p.index, 0);
  r.p.stop();
  r.p.replay(); assert.equal(r.p.index, 0); assert.equal(r.p.playing, true);
});

test("navigating while paused shows the scene but stays paused and silent", () => {
  const r = rig();
  r.p.play(0); r.p.pause();
  const spokenBefore = r.canceled.n;
  r.p.next();
  assert.equal(r.p.index, 1); assert.equal(r.p.playing, false);
  assert.ok(r.canceled.n > spokenBefore, "narration is cancelled, not left speaking");
});

test("narration can be switched off: the timer alone drives playback", () => {
  const r = rig();
  r.p.setNarration(false);
  r.p.play(0);
  assert.deepEqual(r.spoken, []);
  r.advance(4.1);
  assert.equal(r.p.index, 1);
});

test("stale speech callbacks from a skipped scene cannot advance the new scene", () => {
  const r = rig({ speech: "manual" });
  r.p.play(0);
  r.p.next();                         // skip to scene 2 while scene 1's speech is pending
  r.pending[0]();                     // scene 1's narration 'ends' late
  r.advance(6.1);
  assert.equal(r.p.index, 1, "scene 2 is still waiting on its own narration");
  r.pending[1]();
  r.advance(0.2);
  assert.equal(r.p.index, 2);
});

test("scenes that gain illustrations during playback keep the current position", () => {
  const r = rig();
  r.p.play(1);
  r.p.load([{ id: "z", duration: 3, narration: "New", status: "ready", image_url: "/z" }, ...scenes]);
  assert.equal(r.p.scenes[r.p.index].id, "b");
});

test("play() with nothing to play is refused", () => {
  const p = new StoryPlayer(); p.load([]);
  assert.equal(p.play(), false);
});

test("regression: timer functions are not invoked with the player as `this` (browsers throw Illegal invocation)", () => {
  const strict = function (fn) {
    if (this && this.constructor && this.constructor.name === "StoryPlayer") throw new TypeError("Illegal invocation");
    return 1;
  };
  const clear = function () {
    if (this && this.constructor && this.constructor.name === "StoryPlayer") throw new TypeError("Illegal invocation");
  };
  const p = new StoryPlayer({ setInterval: strict, clearInterval: clear, now: () => 0 });
  p.load(scenes);
  assert.doesNotThrow(() => { p.play(0); p.stop(); });
});
