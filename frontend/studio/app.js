// NarraFlow Studio controller: voice session, story stream, tools, playback, UI.
import { VoiceSession, deriveState } from "./protocol.js";
import { checkSupport, createContext, MicCapture, AgentPlayer, MicError } from "./audio.js";
import { StoryPlayer, playableScenes } from "./player.js";

const $ = (id) => document.getElementById(id);
const API = ""; // same origin; the backend serves this page
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

const S = {
  storyId: null, story: null, es: null, voice: null, mic: null, agent: null, ctx: null,
  toolsRunning: 0, lastTool: null, expectRepaintDone: false, expectExport: false,
  transcript: [], partial: "", error: null, demo: false, demoAbort: false,
  followLatest: true, selected: 0, level: 0, micBusy: false, playerOpen: false, narration: true,
};
const player = new StoryPlayer({
  speak: (text, done) => speakText(text, done),
  cancelSpeak: () => { try { speechSynthesis.cancel(); } catch { /* */ } },
});

// ---------------------------------------------------------------- utilities
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};
async function api(path, opts = {}) {
  const res = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
  let body = null; try { body = await res.json(); } catch { /* */ }
  if (!res.ok) { const e = new Error(body?.error || body?.detail || `Request failed (${res.status})`); e.code = body?.code; e.status = res.status; throw e; }
  return body;
}
/** Only messages we wrote (API errors) may reach the user; anything else is logged and replaced. */
function friendly(err, fallback = "Something went wrong. Please try again.") {
  if (err && (err.status || err.code) && err.message) return err.message;
  console.error("[narraflow]", err);
  if (err instanceof TypeError && /fetch|network/i.test(err.message || "")) return "I can't reach the NarraFlow server. Check your connection and try again.";
  return fallback;
}
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(c));
  return n;
}
const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function speakText(text, done) {
  if (!S.narration || !("speechSynthesis" in window)) return done();
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 0.98; u.onend = u.onerror = () => done();
    speechSynthesis.speak(u);
  } catch { done(); }
}

// ---------------------------------------------------------------- errors / banner
function showBanner(message, actions = [], kind = "error") {
  $("banner").className = `banner ${kind === "info" ? "info" : ""}`;
  $("bannerText").textContent = message;
  const box = $("bannerActions"); box.replaceChildren();
  for (const a of actions) box.append(el("button", { class: `btn small ${a.primary ? "primary" : ""}`, type: "button", onclick: a.run, text: a.label }));
  box.append(el("button", { class: "btn small ghost", type: "button", onclick: clearBanner, text: "Dismiss" }));
  $("banner").hidden = false;
}
function clearBanner() { $("banner").hidden = true; S.error = null; render(); }
function fail(message, actions) { S.error = message; showBanner(message, actions); render(); }
const demoAction = { label: "Try the scripted demo", primary: true, run: () => { clearBanner(); runDemo(); } };

// ---------------------------------------------------------------- story state
async function setStory(id, { subscribe = true } = {}) {
  if (S.es) { S.es.close(); S.es = null; }
  S.storyId = id; store.set("narraflow.story", id);
  S.story = await api(`/api/stories/${id}`);
  onStory(null, S.story);
  if (subscribe) {
    S.es = new EventSource(`${API}/api/stories/${id}/events`);
    S.es.onmessage = (e) => { const next = JSON.parse(e.data); const prev = S.story; S.story = next; onStory(prev, next); };
  }
}
async function refreshStory() {
  if (!S.storyId) return;
  try { const next = await api(`/api/stories/${S.storyId}`); if (!S.story || next.version >= S.story.version) { const prev = S.story; S.story = next; onStory(prev, next); } }
  catch { /* the stream will catch up */ }
}
async function newStory() {
  const s = await api("/api/stories", { method: "POST" });
  S.transcript = []; S.partial = ""; S.selected = 0; S.followLatest = true; closePlayer();
  await setStory(s.id);
  return s;
}
function onStory(prev, next) {
  // Reveal: follow the newest illustration as it lands, unless the user picked a scene.
  if (prev && S.followLatest) {
    const just = next.scenes.filter((sc) => sc.status === "ready" && prev.scenes.find((p) => p.id === sc.id)?.status !== "ready");
    if (just.length) S.selected = next.scenes.findIndex((sc) => sc.id === just[just.length - 1].id);
  }
  S.selected = Math.min(S.selected, Math.max(0, next.scenes.length - 1));
  player.load(playableScenes(next));
  // Milestones: let the agent say when a repaint / export finished (only when it is quiet).
  const busy = next.scenes.some((sc) => sc.status === "generating");
  if (S.expectRepaintDone && !busy && prev) {
    S.expectRepaintDone = false;
    if (!next.scenes.some((sc) => sc.status === "failed"))
      S.voice?.nudge("The repainted scenes are now ready on screen. Tell the user so in one short sentence.");
  }
  if (S.expectExport && prev && prev.status === "rendering" && next.status !== "rendering") {
    S.expectExport = false;
    if (next.status === "complete") S.voice?.nudge("The illustrated video is ready to download. Tell the user in one short sentence.");
    else S.voice?.nudge(`The video export failed. Tell the user briefly: ${next.export_error || "something went wrong"}.`);
  }
  if (next.status === "complete" && prev?.status === "rendering") showBanner("Your illustrated story video is ready. Use the Download video button below the canvas.", [], "info");
  if (next.scenes.length && next.scenes.every((sc) => sc.status === "failed") && !S.error) {
    showBanner(next.scenes[0].error || "I couldn't paint your scenes.", [{ label: "Try again", primary: true, run: () => retryScene(1) }]);
  }
  render();
}

// ---------------------------------------------------------------- tools (browser side of the voice agent's tool calls)
async function executeTool(name, args) {
  S.toolsRunning++; S.lastTool = name; render();
  try {
    let result = await api(`/api/tools/${name}`, { method: "POST", body: JSON.stringify({ story_id: S.storyId, arguments: args }) });
    if (result.story_id && result.story_id !== S.storyId) await setStory(result.story_id); // create_story can fork a new story
    else await refreshStory(); // the live stream is eventually consistent; don't act on a stale snapshot
    if (result.ok && ["regenerate_scene", "modify_character", "modify_story_style"].includes(name)) S.expectRepaintDone = true;
    if (result.ok && name === "export_story") S.expectExport = true;
    if (result.ok && result.ui_action === "play") openPlayer(Math.max(0, (result.from_scene || 1) - 1));
    if (!result.ok && result.error) note(`${result.error}`);
    return result;
  } finally { S.toolsRunning--; render(); }
}

// ---------------------------------------------------------------- voice
async function fetchSession(storyId) {
  try {
    return await api("/api/voice/session", { method: "POST", body: JSON.stringify({ story_id: storyId }) });
  } catch (err) {
    err.recoverable = err.code !== "missing_key"; throw err;
  }
}

function buildVoice() {
  const v = new VoiceSession({
    fetchSession, executeTool,
    createSocket: (url) => new WebSocket(url),
    getStoryId: () => S.storyId,
  });
  v.on("phase", () => render());
  v.on("ready", () => { clearBanner(); setLive("sys", "Connected. I'm listening."); render(); });
  v.on("speech", () => render());
  v.on("transcript.user.delta", ({ text }) => { S.partial = text; setLive("user", text, true); });
  v.on("transcript.user", ({ text }) => { S.partial = ""; addTurn("user", text); setLive("user", text); });
  v.on("transcript.agent", ({ text, interrupted }) => { addTurn("agent", text + (interrupted ? " …" : "")); setLive("agent", text); });
  v.on("agent.audio", ({ data }) => S.agent?.enqueue(data));
  v.on("agent.flush", () => S.agent?.flush());
  v.on("reply", () => render());
  v.on("tool.dropped", () => note("I made that change, but got cut off before I could tell you."));
  v.on("notice", ({ message }) => note(message));
  v.on("reconnecting", ({ attempt }) => setLive("sys", `Connection dropped. Reconnecting (${attempt})…`));
  v.on("error", ({ message, recoverable, code }) => { stopAudio(); S.voice = null; fail(message, code === "missing_key" ? [demoAction] : recoverable ? [{ label: "Try again", primary: true, run: () => startVoice() }, demoAction] : [demoAction]); });
  v.on("ended", () => { stopAudio(); render(); });
  return v;
}

function stopAudio() {
  S.mic?.stop(); S.mic = null; S.agent?.flush();
  if (S.ctx) { S.ctx.close().catch(() => {}); S.ctx = null; } S.agent = null; S.level = 0;
}

async function startVoice() {
  if (S.micBusy || S.voice) return;
  clearBanner();
  const problem = checkSupport();
  if (problem) return fail(problem, [demoAction]);
  S.micBusy = true; render();
  try {
    if (!S.storyId) await newStory();
    S.ctx = createContext();
    S.agent = new AgentPlayer(S.ctx, { onSpeaking: () => render() });
    S.voice = buildVoice();
    S.mic = new MicCapture(S.ctx, { onChunk: (b64) => S.voice?.sendAudio(b64), onLevel: (l) => { S.level = l; } });
    await S.mic.start(); // getUserMedia must run inside the click gesture
    await S.voice.start();
  } catch (err) {
    stopAudio(); S.voice = null;
    const mic = err instanceof MicError;
    fail(err instanceof MicError ? err.message : friendly(err, "I couldn't start the voice session."), mic ? [{ label: "Try again", primary: true, run: () => startVoice() }, demoAction] : [demoAction]);
  } finally { S.micBusy = false; render(); }
}

function stopVoice() {
  S.voice?.end(); S.voice = null; stopAudio(); S.partial = "";
  setLive("sys", "Paused. Press the mic to keep going."); render();
}
const toggleVoice = () => (S.voice ? stopVoice() : startVoice());

// ---------------------------------------------------------------- transcript
function addTurn(role, text) {
  if (!text?.trim()) return;
  S.transcript.push({ role, text: text.trim(), demo: S.demo });
  renderLog();
}
function note(text) { setLive("sys", text); S.transcript.push({ role: "sys", text }); renderLog(); }
function setLive(role, text, partial = false) {
  const who = $("liveWho"); who.className = `who ${role}`;
  who.textContent = role === "user" ? (S.demo ? "You (demo)" : "You") : role === "agent" ? (S.demo ? "NarraFlow (demo)" : "NarraFlow") : "NarraFlow";
  const t = $("liveTxt"); t.textContent = text; t.className = `txt ${partial ? "partial" : ""}`;
}

// ---------------------------------------------------------------- player
function openPlayer(from = 0) {
  if (!player.scenes.length) { note("Nothing is illustrated yet. Give me a moment to finish painting."); return; }
  S.playerOpen = true; S.voice?.setMuted(true); // don't let the narration talk to the agent
  player.play(from); render();
}
function closePlayer() {
  if (!S.playerOpen) return;
  S.playerOpen = false; player.stop(); S.voice?.setMuted(false); render();
}
player.on("scene", ({ index, scene }) => { const i = S.story.scenes.findIndex((s) => s.id === scene.id); S.selected = i < 0 ? 0 : i; S.followLatest = false; render(); showScene(scene, true); });
player.on("progress", ({ elapsed, total }) => { $("pProg").style.width = `${total ? (elapsed / total) * 100 : 0}%`; $("pTime").textContent = `${fmt(elapsed)} / ${fmt(total)}`; });
player.on("state", () => { $("pPlay").textContent = player.playing ? "⏸" : "▶"; render(); });
player.on("end", () => { $("pPlay").textContent = "↺"; $("pProg").style.width = "100%"; S.voice?.setMuted(false); });

// ---------------------------------------------------------------- retry
async function retryScene(n) {
  const r = await api("/api/tools/generate_scene", { method: "POST", body: JSON.stringify({ story_id: S.storyId, arguments: { scene_number: n, force: true } }) });
  if (!r.ok) note(r.error);
}

// ---------------------------------------------------------------- rendering
function currentScene() { return S.story?.scenes[S.selected] || null; }

function showScene(scene, kenBurns) {
  const img = $("sceneImg");
  if (!scene || scene.status !== "ready" || !scene.image_url) return;
  if (img.dataset.src !== scene.image_url || kenBurns) {
    const fresh = img.cloneNode(false); // restart CSS animations cleanly
    fresh.id = "sceneImg"; fresh.hidden = false; fresh.dataset.src = scene.image_url; fresh.alt = scene.summary || `Scene ${scene.order}`;
    fresh.className = "scene";
    if (!REDUCED) {
      if (kenBurns) { fresh.classList.add("kb"); fresh.style.setProperty("--kb-dur", `${scene.duration + 2}s`); fresh.style.setProperty("--kb-origin", scene.order % 2 ? "30% 60%" : "70% 40%"); fresh.style.setProperty("--kb-to", scene.order % 3 === 0 ? "1.06" : "1.12"); }
      else fresh.classList.add("reveal");
    }
    fresh.src = scene.image_url;
    img.replaceWith(fresh);
  }
}

function render() {
  const story = S.story;
  const scenes = story?.scenes || [];
  const sc = currentScene();
  const voice = S.voice ? { phase: S.voice.phase, userSpeaking: S.voice.userSpeaking, agentSpeaking: !!S.agent?.speaking } : {};
  const st = deriveState({ voice, story, toolsRunning: S.toolsRunning, lastTool: S.lastTool, playing: S.playerOpen && player.playing, error: S.error });
  const status = $("status"); status.dataset.state = st.state; $("statusLabel").textContent = S.demo && st.state === "idle" ? "Scripted demo" : st.label;

  $("storyTitle").textContent = story?.title && story.title !== "Untitled Story" ? story.title : "A new story";
  $("storySub").textContent = [story?.genre, story?.tone].filter(Boolean).join(" · ");

  // canvas
  const has = scenes.length > 0;
  $("empty").hidden = has; $("cap").hidden = true;
  const generating = sc && sc.status === "generating", failed = sc && sc.status === "failed", pending = sc && sc.status === "pending";
  $("skeleton").hidden = !(sc && (generating || pending));
  $("skeletonText").textContent = sc ? (sc.revision > 0 ? `Repainting scene ${sc.order}…` : `Painting scene ${sc.order}…`) : "";
  $("failed").hidden = !failed; if (failed) $("failedText").textContent = sc.error || "That scene didn't paint.";
  $("sceneImg").hidden = !(sc && sc.status === "ready");
  if (sc && sc.status === "ready" && !S.playerOpen) showScene(sc, false);
  const badge = $("badge"); badge.hidden = !sc;
  if (sc) badge.textContent = `Scene ${sc.order} of ${scenes.length}${sc.revision ? ` · revision ${sc.revision}` : ""}`;
  if (S.playerOpen && sc) { $("cap").hidden = false; $("cap").textContent = sc.narration || sc.summary; }
  $("playerBar").hidden = !S.playerOpen; $("canvas").classList.toggle("has-bar", S.playerOpen);
  $("pNarr").setAttribute("aria-pressed", String(S.narration)); $("pNarr").textContent = S.narration ? "🔊" : "🔇";

  // dock
  const mic = $("mic"); const on = !!S.voice;
  mic.setAttribute("aria-pressed", String(on)); mic.dataset.busy = String(S.micBusy); mic.dataset.agent = voice.agentSpeaking ? "speaking" : "";
  mic.setAttribute("aria-label", on ? "Stop the conversation" : "Start telling your story");
  $("micIcon").textContent = on ? "■" : "🎙";
  $("dockTitle").textContent = st.label;
  $("dockSub").textContent = S.demo ? "Scripted demo: the same tools the voice agent uses, no microphone involved."
    : on ? "Speak naturally. Interrupt me any time. Press ■ to stop." : "Press the microphone (or Space) to start.";
  const ready = playableScenes(story).length;
  $("btnPlay").disabled = !ready; $("btnExport").disabled = !ready || story?.status === "rendering";
  $("btnExport").textContent = story?.status === "rendering" ? "Rendering…" : "Export video";
  const dl = $("btnDownload"); dl.hidden = !story?.export_url; if (story?.export_url) dl.href = story.export_url;

  // side panels
  $("nScenes").textContent = scenes.length; $("nChars").textContent = story?.characters.length || 0;
  renderScenes(scenes); renderChars(story?.characters || []); renderStory(story);
}

function renderScenes(scenes) {
  const box = $("sceneList");
  if (!scenes.length) { box.replaceChildren(el("p", { class: "muted", text: "Scenes appear here as you narrate." })); return; }
  box.replaceChildren(...scenes.map((s, i) => {
    const th = el("div", { class: `th ${s.status === "ready" ? "" : s.status === "failed" ? "" : "busy"}` });
    if (s.status === "ready" && s.image_url) th.style.backgroundImage = `url("${s.image_url}")`;
    const tag = s.status === "ready" ? el("span", { class: "tag ok", text: "ready" }) : s.status === "failed" ? el("span", { class: "tag bad", text: "failed" }) : el("span", { class: "tag busy", text: "painting" });
    return el("button", { class: "scard", type: "button", "aria-current": String(i === S.selected), onclick: () => { S.selected = i; S.followLatest = i === scenes.length - 1; render(); } },
      th, el("div", { class: "meta" }, el("b", { text: `Scene ${s.order}` }), tag, el("p", { text: s.summary || s.narration })));
  }));
}
function renderChars(chars) {
  const box = $("charList");
  if (!chars.length) { box.replaceChildren(el("p", { class: "muted", text: "Characters keep the same look in every scene." })); return; }
  box.replaceChildren(...chars.map((c) => {
    const av = el("div", { class: "av" }); if (c.portrait_url) av.style.backgroundImage = `url("${c.portrait_url}")`;
    const traits = (c.persistent_visual_traits || []).map((t) => el("span", { text: t }));
    return el("div", { class: "char" }, av, el("div", {}, el("b", { text: c.name }), el("div", { class: "muted", text: c.description || "" }), el("div", { class: "traits" }, traits)));
  }));
}
function renderStory(story) {
  const scenes = story?.scenes || [];
  const done = scenes.filter((s) => s.status === "ready").length;
  $("progBar").style.width = scenes.length ? `${(done / scenes.length) * 100}%` : "0";
  $("progText").textContent = scenes.length ? `${done} of ${scenes.length} scenes illustrated` : "No scenes yet.";
  const kv = $("storyKv"); kv.replaceChildren();
  if (story) for (const [k, v] of [["Style", story.visual_style], ["Tone", story.tone], ["Genre", story.genre], ["Language", story.language]])
    if (v) kv.append(el("dt", { text: k }), el("dd", { text: v }));
  const dl = $("downloads"); dl.replaceChildren();
  if (story?.export_url) dl.append(el("a", { class: "btn small primary", href: story.export_url, download: "" , text: "Download video" }), el("a", { class: "btn small", href: `/api/stories/${story.id}/captions.srt`, text: "Captions (.srt)" }));
  if (story?.export_error) dl.append(el("span", { class: "muted", text: story.export_error }));
}
function renderLog() {
  const box = $("log"); $("nLog").textContent = S.transcript.length;
  if (!S.transcript.length) return;
  box.replaceChildren(...S.transcript.map((t) => el("div", { class: "row" }, el("span", { class: `who ${t.role}`, text: t.role === "user" ? "You" : t.role === "agent" ? "NarraFlow" : "Note" }), el("span", { text: t.text }))));
  box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- waveform
const wave = $("wave"), wctx = wave.getContext("2d"); let phase = 0;
function drawWave() {
  const w = wave.width, h = wave.height; wctx.clearRect(0, 0, w, h);
  const speaking = S.agent?.speaking;
  const level = speaking ? S.agent.level() * 3 : Math.min(1, S.level * 6);
  const active = !!S.voice; const bars = 44; phase += 0.09;
  const color = speaking ? "#a78bfa" : "#5fd6c4";
  for (let i = 0; i < bars; i++) {
    const base = active ? 0.06 + level * (0.5 + 0.5 * Math.sin(phase * 2 + i * 0.55)) : 0.05 + 0.02 * Math.sin(phase + i);
    const bh = Math.max(3, Math.min(h, base * h * 1.6));
    wctx.fillStyle = active ? color : "#4a4270";
    wctx.globalAlpha = active ? 0.55 + 0.45 * Math.min(1, level * 2) : 0.6;
    wctx.fillRect(i * (w / bars) + 2, (h - bh) / 2, w / bars - 5, bh);
  }
  requestAnimationFrame(drawWave);
}

// ---------------------------------------------------------------- scripted demo (same tools, no microphone)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitForPainting(timeout = 45000) {
  const end = Date.now() + timeout;
  while (Date.now() < end && !S.demoAbort) {
    if (!S.story?.scenes.some((s) => s.status === "generating" || s.status === "pending")) return;
    await sleep(300);
  }
}
async function runDemo() {
  if (S.demo) return;
  if (S.voice) stopVoice();
  S.demo = true; S.demoAbort = false; clearBanner();
  try {
    const steps = await api("/api/demo/script");
    await newStory();
    showBanner("This is a scripted demo. It replays a short original story through the same tools the voice agent uses, so you can see the whole flow without a microphone.", [], "info");
    for (const step of steps) {
      if (S.demoAbort) break;
      addTurn("user", step.user); setLive("user", step.user); await sleep(1300);
      const result = await executeTool(step.tool, step.args);
      if (!result.ok) { note(result.error || "A demo step failed."); break; }
      addTurn("agent", step.agent); setLive("agent", step.agent);
      if (step.tool !== "play_story") await waitForPainting();
      else await sleep(600);
      await sleep(step.tool === "regenerate_scene" ? 900 : 500);
    }
  } catch (err) {
    fail(friendly(err, "The demo couldn't run."), [{ label: "Try again", primary: true, run: () => { S.demo = false; runDemo(); } }]);
  } finally { S.demo = false; render(); }
}

// ---------------------------------------------------------------- wiring
$("mic").addEventListener("click", toggleVoice);
$("btnDemo").addEventListener("click", () => { if (S.demo) { S.demoAbort = true; } else runDemo(); });
$("btnNew").addEventListener("click", async () => { S.demoAbort = true; if (S.voice) stopVoice(); await newStory(); setLive("sys", "A fresh page. Press the mic when you're ready."); });
$("btnPlay").addEventListener("click", () => openPlayer(0));
$("btnRetry").addEventListener("click", () => { const s = currentScene(); if (s) retryScene(s.order); });
$("btnExport").addEventListener("click", async () => {
  try { const r = await api(`/api/stories/${S.storyId}/export`, { method: "POST" }); if (!r.ok) note(r.error); else S.expectExport = true; }
  catch (e) { note(friendly(e)); }
});
$("pPlay").addEventListener("click", () => (player.index >= player.scenes.length - 1 && !player.playing && $("pProg").style.width === "100%" ? player.replay() : player.toggle()));
$("pPrev").addEventListener("click", () => player.prev()); $("pNext").addEventListener("click", () => player.next());
$("pReplay").addEventListener("click", () => player.replay());
$("pNarr").addEventListener("click", () => { S.narration = !S.narration; player.setNarration(S.narration); render(); });
$("pClose").addEventListener("click", closePlayer);
addEventListener("keydown", (e) => {
  if (e.target.closest("input,textarea,select,[contenteditable]") || e.target.closest("button, a, summary")) return;
  if (e.code === "Space") { e.preventDefault(); toggleVoice(); }
  else if (e.key === "Escape") closePlayer();
});
addEventListener("beforeunload", () => { try { S.voice?.end(); } catch { /* */ } });

(async function init() {
  drawWave(); render();
  const params = new URLSearchParams(location.search);
  try {
    const health = await api("/api/health");
    if (!health.voice_configured) showBanner("Voice isn't set up on this server yet (no AssemblyAI key). The scripted demo shows the full flow.", [demoAction], "info");
    const wanted = params.get("story") || store.get("narraflow.story");
    if (wanted) { try { await setStory(wanted); if (S.story.scenes.length) setLive("sys", `Welcome back to “${S.story.title}”. Press the mic to continue.`); } catch { store.set("narraflow.story", null); } }
    if (!S.storyId && params.get("demo") !== "1") await newStory();
    if (params.get("demo") === "1") runDemo();
  } catch (err) {
    fail("I can't reach the NarraFlow server. Make sure it's running, then reload.", [{ label: "Reload", primary: true, run: () => location.reload() }]);
  }
})();
