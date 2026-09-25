import test from "node:test";
import assert from "node:assert/strict";
import {
  VoiceSession, deriveState, floatTo16BitPCM, int16ToBase64, base64ToInt16, resample, humanError,
} from "../protocol.js";

class FakeSocket {
  constructor(url) { this.url = url; this.readyState = 0; this.sent = []; FakeSocket.all.push(this); }
  open() { this.readyState = 1; this.onopen && this.onopen(); }
  send(s) { this.sent.push(JSON.parse(s)); }
  close() { if (this.readyState === 3) return; this.readyState = 3; this.onclose && this.onclose(); }
  serverSays(obj) { this.onmessage({ data: JSON.stringify(obj) }); }
  types() { return this.sent.map((m) => m.type); }
}
FakeSocket.all = [];

const SESSION = { system_prompt: "p", greeting: "hi", tools: [{ type: "function", name: "t" }] };

function make(overrides = {}) {
  FakeSocket.all = [];
  const timers = [];
  const calls = { fetch: 0, tools: [] };
  const s = new VoiceSession({
    fetchSession: async () => { calls.fetch++; return { token: `tok${calls.fetch}`, ws_url: "wss://agents.assemblyai.com/v1/ws", session: SESSION }; },
    executeTool: async (name, args) => { calls.tools.push([name, args]); return { ok: true, name }; },
    createSocket: (url) => new FakeSocket(url),
    getStoryId: () => "sty_1",
    setTimer: (fn) => { timers.push(fn); return timers.length; },
    clearTimer: () => {},
    ...overrides,
  });
  const events = [];
  for (const t of ["phase", "ready", "error", "notice", "agent.flush", "agent.audio", "transcript.user", "transcript.user.delta",
    "transcript.agent", "tool.started", "tool.finished", "tool.dropped", "reconnecting", "speech", "reply", "ended"]) {
    s.on(t, (d) => events.push([t, d]));
  }
  return { s, timers, calls, events, sock: () => FakeSocket.all.at(-1), flushTimers: () => { const f = timers.splice(0); f.forEach((fn) => fn()); } };
}

async function ready(ctx, id = "sess_1") {
  await ctx.s.start();
  ctx.sock().open();
  ctx.sock().serverSays({ type: "session.ready", session_id: id });
}
const tick = () => new Promise((r) => setTimeout(r, 0));

test("connects with a single-use token in the URL and sends session.update first", async () => {
  const c = make();
  await c.s.start();
  const url = new URL(c.sock().url);
  assert.equal(url.origin + url.pathname, "wss://agents.assemblyai.com/v1/ws");
  assert.equal(url.searchParams.get("token"), "tok1");
  assert.equal(c.s.phase, "connecting");
  c.sock().open();
  assert.deepEqual(c.sock().sent, [{ type: "session.update", session: SESSION }]);
});

test("no microphone audio is sent before session.ready", async () => {
  const c = make();
  await c.s.start();
  c.sock().open();
  assert.equal(c.s.sendAudio("AAAA"), false);
  assert.ok(!c.sock().types().includes("input.audio"));
  c.sock().serverSays({ type: "session.ready", session_id: "s1" });
  assert.equal(c.s.phase, "ready");
  assert.equal(c.s.sendAudio("AAAA"), true);
  assert.deepEqual(c.sock().sent.at(-1), { type: "input.audio", audio: "AAAA" });
});

test("muting stops audio (used while the story plays back)", async () => {
  const c = make(); await ready(c);
  c.s.setMuted(true);
  assert.equal(c.s.sendAudio("AAAA"), false);
  c.s.setMuted(false);
  assert.equal(c.s.sendAudio("AAAA"), true);
});

test("relays transcripts and agent audio", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "transcript.user.delta", text: "Once upon" });
  c.sock().serverSays({ type: "transcript.user", text: "Once upon a time", item_id: "i1" });
  c.sock().serverSays({ type: "reply.started", reply_id: "r1" });
  c.sock().serverSays({ type: "reply.audio", data: "QUJD" });
  c.sock().serverSays({ type: "transcript.agent", text: "Lovely.", reply_id: "r1", interrupted: false });
  const by = Object.fromEntries(c.events.map(([t, d]) => [t, d]));
  assert.equal(by["transcript.user.delta"].text, "Once upon");
  assert.equal(by["transcript.user"].text, "Once upon a time");
  assert.equal(by["agent.audio"].data, "QUJD");
  assert.equal(by["transcript.agent"].text, "Lovely.");
});

test("tool results are sent AFTER reply.done, as JSON strings, batched", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "reply.started", reply_id: "r1" });
  c.sock().serverSays({ type: "tool.call", call_id: "c1", name: "add_story_content", arguments: { narration_text: "x" } });
  c.sock().serverSays({ type: "tool.call", call_id: "c2", name: "play_story", arguments: "{\"from_scene\": 2}" });
  await tick();
  assert.ok(!c.sock().types().includes("tool.result"), "must not send results before reply.done");
  assert.deepEqual(c.calls.tools, [["add_story_content", { narration_text: "x" }], ["play_story", { from_scene: 2 }]]);
  c.sock().serverSays({ type: "reply.done", status: "completed" });
  await tick(); await tick();
  const results = c.sock().sent.filter((m) => m.type === "tool.result");
  assert.equal(results.length, 2);
  assert.deepEqual(results.map((r) => r.call_id), ["c1", "c2"]);
  for (const r of results) assert.equal(typeof r.result, "string", "result must be a JSON-encoded string");
  assert.deepEqual(JSON.parse(results[0].result), { ok: true, name: "add_story_content" });
});

test("a slow tool still gets delivered after reply.done", async () => {
  let release;
  const c = make({ executeTool: () => new Promise((r) => { release = () => r({ ok: true }); }) });
  await ready(c);
  c.sock().serverSays({ type: "tool.call", call_id: "c1", name: "export_story", arguments: {} });
  c.sock().serverSays({ type: "reply.done", status: "completed" });
  await tick();
  assert.ok(!c.sock().types().includes("tool.result"));
  release(); await tick(); await tick();
  assert.equal(c.sock().sent.filter((m) => m.type === "tool.result").length, 1);
});

test("a failing tool still answers the agent with a readable error", async () => {
  const c = make({ executeTool: async () => { throw new Error("boom internal"); } });
  await ready(c);
  c.sock().serverSays({ type: "tool.call", call_id: "c1", name: "x", arguments: {} });
  c.sock().serverSays({ type: "reply.done", status: "completed" });
  await tick(); await tick();
  const r = JSON.parse(c.sock().sent.find((m) => m.type === "tool.result").result);
  assert.equal(r.ok, false);
  assert.ok(!r.error.includes("boom internal"));
});

test("interruption: pending tool results are discarded and the speaker is flushed", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "reply.started", reply_id: "r1" });
  c.sock().serverSays({ type: "tool.call", call_id: "c1", name: "regenerate_scene", arguments: {} });
  c.sock().serverSays({ type: "reply.done", status: "interrupted" });
  await tick(); await tick();
  assert.ok(!c.sock().types().includes("tool.result"));
  assert.ok(c.events.some(([t, d]) => t === "agent.flush" && d.reason === "interrupted"));
  assert.ok(c.events.some(([t]) => t === "tool.dropped"), "the UI is told an action ran without the agent hearing back");
  assert.equal(c.s.pending.length, 0);
});

test("barge-in: user speech while the agent is talking flushes local audio immediately", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "reply.started", reply_id: "r1" });
  c.sock().serverSays({ type: "input.speech.started" });
  assert.ok(c.events.some(([t, d]) => t === "agent.flush" && d.reason === "barge-in"));
  assert.equal(c.s.userSpeaking, true);
  c.sock().serverSays({ type: "input.speech.stopped" });
  assert.equal(c.s.userSpeaking, false);
});

test("audio still in flight from an interrupted reply is dropped, and the next reply plays normally", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "reply.started", reply_id: "r1" });
  c.sock().serverSays({ type: "reply.audio", data: "AAAA" });
  c.sock().serverSays({ type: "input.speech.started" });           // barge-in
  c.sock().serverSays({ type: "reply.audio", data: "BBBB" });       // late chunk of the cut-off reply
  c.sock().serverSays({ type: "reply.audio", data: "CCCC" });
  assert.deepEqual(c.events.filter(([t]) => t === "agent.audio").map(([, d]) => d.data), ["AAAA"]);
  c.sock().serverSays({ type: "reply.done", status: "interrupted" });
  c.sock().serverSays({ type: "reply.audio", data: "DDDD" });       // still the old reply
  assert.equal(c.events.filter(([t]) => t === "agent.audio").length, 1);
  c.sock().serverSays({ type: "reply.started", reply_id: "r2" });   // the agent answers the interruption
  c.sock().serverSays({ type: "reply.audio", data: "EEEE" });
  assert.deepEqual(c.events.filter(([t]) => t === "agent.audio").map(([, d]) => d.data), ["AAAA", "EEEE"]);
});

test("no flush when the user speaks and the agent is silent", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "input.speech.started" });
  assert.ok(!c.events.some(([t]) => t === "agent.flush"));
});

test("nudges wait for a quiet moment", async () => {
  const c = make(); await ready(c);
  assert.equal(c.s.nudge("Say scene 3 is ready."), true);
  assert.deepEqual(c.sock().sent.at(-1), { type: "reply.create", instructions: "Say scene 3 is ready." });
  c.sock().serverSays({ type: "reply.started", reply_id: "r1" });
  assert.equal(c.s.nudge("Later one"), false);
  const before = c.sock().sent.length;
  c.sock().serverSays({ type: "reply.done", status: "completed" });
  await tick();
  assert.equal(c.sock().sent.length, before + 1);
  assert.equal(c.sock().sent.at(-1).instructions, "Later one");
});

test("nudge is held while the user is speaking", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "input.speech.started" });
  assert.equal(c.s.nudge("hey"), false);
  assert.ok(!c.sock().types().includes("reply.create"));
  c.sock().serverSays({ type: "input.speech.stopped" });
  assert.equal(c.sock().sent.at(-1).type, "reply.create");
});

test("dropped connection: reconnect with a FRESH token and session.resume", async () => {
  const c = make(); await ready(c, "sess_abc");
  const first = c.sock();
  first.close(); // network drop
  assert.equal(c.s.phase, "reconnecting");
  assert.ok(c.events.some(([t, d]) => t === "reconnecting" && d.attempt === 1));
  c.flushTimers(); await tick(); await tick();
  const second = c.sock();
  assert.notEqual(second, first);
  assert.equal(new URL(second.url).searchParams.get("token"), "tok2", "tokens are single-use: a new one is needed");
  second.open();
  assert.deepEqual(second.sent, [{ type: "session.resume", session_id: "sess_abc" }]);
  second.serverSays({ type: "session.ready", session_id: "sess_abc" });
  assert.equal(c.s.phase, "ready");
  assert.equal(c.s.sendAudio("AAAA"), true);
});

test("session_expired on resume falls back to a brand-new session", async () => {
  const c = make(); await ready(c, "sess_old");
  c.sock().close(); c.flushTimers(); await tick(); await tick();
  const resumed = c.sock(); resumed.open();
  resumed.serverSays({ type: "session.error", code: "session_expired", message: "expired" });
  assert.ok(c.events.some(([t, d]) => t === "notice" && /fresh one/.test(d.message)));
  c.flushTimers(); await tick(); await tick();
  const fresh = c.sock(); fresh.open();
  assert.equal(fresh.sent[0].type, "session.update", "starts fresh, not another resume");
});

test("gives up after repeated failures with a recoverable, readable error", async () => {
  const c = make({ maxReconnects: 2 });
  await c.s.start(); c.sock().close();
  c.flushTimers(); await tick(); await tick(); c.sock().close();
  c.flushTimers(); await tick(); await tick(); c.sock().close();
  const err = c.events.find(([t]) => t === "error");
  assert.ok(err, "an error event is emitted");
  assert.equal(err[1].recoverable, true);
  assert.match(err[1].message, /Press the mic to try again/);
  assert.equal(c.s.phase, "error");
});

test("token endpoint failure (e.g. missing key) surfaces its message, not a stack trace", async () => {
  const boom = Object.assign(new Error("Voice is not set up on this server yet."), { code: "missing_key", recoverable: false });
  const c = make({ fetchSession: async () => { throw boom; } });
  await c.s.start();
  const err = c.events.find(([t]) => t === "error")[1];
  assert.equal(err.code, "missing_key");
  assert.match(err.message, /not set up/);
  assert.equal(FakeSocket.all.length, 0, "no socket is opened without a token");
});

test("auth errors stop the session and do not loop", async () => {
  const c = make(); await c.s.start(); c.sock().open();
  c.sock().serverSays({ type: "session.error", code: "UNAUTHORIZED", message: "bad" });
  assert.equal(c.s.phase, "error");
  assert.equal(c.timers.length, 0, "no reconnect timer is scheduled");
  assert.equal(c.events.find(([t]) => t === "error")[1].message, humanError("UNAUTHORIZED"));
});

test("non-fatal server errors are surfaced as notices only", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "session.error", code: "invalid_audio", message: "x" });
  assert.equal(c.s.phase, "ready");
  assert.ok(c.events.some(([t]) => t === "notice"));
});

test("end() sends session.end, closes, and never reconnects", async () => {
  const c = make(); await ready(c);
  const sock = c.sock();
  c.s.end();
  assert.equal(sock.sent.at(-1).type, "session.end");
  assert.equal(c.s.phase, "closed");
  c.flushTimers();
  assert.equal(FakeSocket.all.length, 1);
});

test("pending tools are dropped when the connection dies", async () => {
  const c = make(); await ready(c);
  c.sock().serverSays({ type: "tool.call", call_id: "c1", name: "x", arguments: {} });
  c.sock().close();
  assert.equal(c.s.pending.length, 0);
});

// ---- state machine ---------------------------------------------------------
const sc = (o) => ({ order: 1, status: "ready", revision: 0, ...o });
test("deriveState covers the required states with human labels", () => {
  assert.deepEqual(deriveState({}), { state: "idle", label: "Press the microphone to begin" });
  assert.equal(deriveState({ error: "Mic blocked" }).state, "error");
  assert.equal(deriveState({ voice: { phase: "connecting" } }).state, "processing");
  assert.equal(deriveState({ voice: { phase: "ready" } }).state, "listening");
  assert.equal(deriveState({ voice: { phase: "ready", userSpeaking: true } }).label, "Listening...");
  assert.equal(deriveState({ voice: { phase: "ready" }, toolsRunning: 1 }).label, "Understanding your story...");
  const gen = deriveState({ voice: { phase: "ready" }, story: { scenes: [sc({ order: 2, status: "generating" })] } });
  assert.deepEqual([gen.state, gen.label], ["generating", "Creating scene 2..."]);
  const edit = deriveState({ voice: { phase: "ready" }, lastTool: "regenerate_scene", story: { scenes: [sc({ order: 3, status: "generating", revision: 1 })] } });
  assert.deepEqual([edit.state, edit.label], ["editing", "Repainting scene 3..."]);
  assert.match(deriveState({ lastTool: "modify_character", story: { scenes: [sc({ status: "generating" })] } }).label, /consistent/);
  assert.equal(deriveState({ story: { status: "rendering", scenes: [] } }).state, "rendering");
  assert.equal(deriveState({ voice: { phase: "ready", agentSpeaking: true } }).state, "speaking");
  assert.equal(deriveState({ story: { status: "complete", scenes: [sc({})] } }).state, "complete");
  assert.equal(deriveState({ story: { scenes: [sc({})] } }).state, "ready");
});

test("state priority: the user speaking beats background painting; errors beat everything", () => {
  const painting = { scenes: [sc({ status: "generating" })] };
  assert.equal(deriveState({ voice: { phase: "ready", userSpeaking: true }, story: painting }).state, "listening");
  assert.equal(deriveState({ voice: { phase: "ready", userSpeaking: true }, story: painting, error: "x" }).state, "error");
});

// ---- audio helpers ---------------------------------------------------------
test("PCM16 conversion clamps and round-trips through base64", () => {
  const pcm = floatTo16BitPCM(new Float32Array([0, 1, -1, 2, -2, 0.5]));
  assert.deepEqual([...pcm], [0, 32767, -32768, 32767, -32768, 16383]);
  assert.deepEqual([...base64ToInt16(int16ToBase64(pcm))], [...pcm]);
  const big = new Int16Array(100000).map((_, i) => (i % 2000) - 1000);
  assert.deepEqual([...base64ToInt16(int16ToBase64(big))], [...big], "large chunks survive (no call-stack overflow)");
});

test("resample changes length by the rate ratio and keeps a DC signal", () => {
  const out = resample(new Float32Array(4800).fill(0.25), 48000, 24000);
  assert.equal(out.length, 2400);
  assert.ok(out.every((v) => Math.abs(v - 0.25) < 1e-6));
  assert.equal(resample(new Float32Array(10), 24000, 24000).length, 10);
});
