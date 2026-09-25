// AssemblyAI Voice Agent API client (browser + node-testable).
//
// Pure protocol logic: no DOM, no audio APIs. The WebSocket and the backend
// calls are injected, so the same code runs in the page and in node tests.
//
// Flow (see https://www.assemblyai.com/docs/voice-agents/voice-agent-api):
//   POST /api/voice/session  -> single-use token + session config (key stays on the server)
//   open  wss://agents.assemblyai.com/v1/ws?token=...
//   send  session.update     (system prompt, greeting, turn detection, tools)
//   wait  session.ready      -> only then stream input.audio (PCM16, 24 kHz, base64)
//   recv  transcript.user(.delta), reply.audio, transcript.agent, tool.call, reply.done ...
//   tools: run on tool.call, send tool.result AFTER reply.done (discard if interrupted)
//   drop:  reconnect with a fresh token + session.resume within 30 s, else start fresh.

export const HUMAN_ERRORS = {
  UNAUTHORIZED: "The voice service didn't accept this session. Please try again.",
  FORBIDDEN: "The voice service refused this session. Please try again.",
  INTERNAL_ERROR: "The voice service had a hiccup. Reconnecting.",
  server_error: "The voice service had a hiccup. Reconnecting.",
  session_not_found: "That voice session is gone, so I'm starting a fresh one.",
  session_expired: "The voice session timed out, so I'm starting a fresh one.",
  session_forbidden: "The voice service refused to resume that session.",
  agent_init_failed: "The voice agent couldn't start. Please try again.",
  agent_timeout: "The voice agent took too long to answer. Please try again.",
  invalid_config: "The voice agent's configuration was rejected.",
  invalid_audio: "The microphone audio wasn't accepted.",
  invalid_format: "The microphone audio format wasn't accepted.",
  invalid_value: "The voice agent's configuration had an invalid value.",
  immutable_field: "The voice agent's configuration had a locked field.",
};
const FRESH_SESSION_CODES = new Set(["session_not_found", "session_expired", "session_forbidden"]);

export function humanError(code, message) {
  return HUMAN_ERRORS[code] || message || "Something went wrong with the voice connection.";
}

export class Emitter {
  constructor() { this._h = {}; }
  on(type, fn) { (this._h[type] ||= []).push(fn); return () => this.off(type, fn); }
  off(type, fn) { this._h[type] = (this._h[type] || []).filter((f) => f !== fn); }
  emit(type, detail) { for (const fn of [...(this._h[type] || [])]) fn(detail); }
}

export class VoiceSession extends Emitter {
  /**
   * @param {object} o
   * @param {(storyId:string)=>Promise<{token:string, ws_url:string, session:object}>} o.fetchSession
   * @param {(name:string, args:object)=>Promise<object>} o.executeTool
   * @param {(url:string)=>WebSocketLike} o.createSocket
   * @param {()=>string|null} o.getStoryId
   * @param {(fn:Function, ms:number)=>any} [o.setTimer]
   */
  constructor({ fetchSession, executeTool, createSocket, getStoryId, setTimer, clearTimer, maxReconnects = 3 }) {
    super();
    this.fetchSession = fetchSession;
    this.executeTool = executeTool;
    this.createSocket = createSocket;
    this.getStoryId = getStoryId;
    this.setTimer = setTimer || ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = clearTimer || ((t) => clearTimeout(t));
    this.maxReconnects = maxReconnects;
    this.phase = "idle"; // idle | connecting | ready | reconnecting | closed | error
    this.sessionId = null;
    this.userSpeaking = false;
    this.agentSpeaking = false; // a reply is in flight (started, not done)
    this.muted = false;
    this.pending = []; // in-flight tool executions for the current reply
    this._ws = null;
    this._wantOpen = false;
    this._resumeFrom = null;
    this._attempt = 0;
    this._greetingHeard = false;
    this._pendingNudge = null;
    this._session = null;
    this._dropAudio = false; // true after a barge-in/interruption, until the next reply starts
  }

  // ---- lifecycle -------------------------------------------------------
  async start() {
    this._wantOpen = true;
    this._attempt = 0;
    await this._connect(false);
  }

  async _connect(resume) {
    this._setPhase(resume ? "reconnecting" : "connecting");
    let info;
    try {
      info = await this.fetchSession(this.getStoryId());
    } catch (err) {
      return this._fail(err.message || "I couldn't start the voice session.", err.code || "session_failed", !!err.recoverable);
    }
    this._session = info.session;
    const url = new URL(info.ws_url);
    url.searchParams.set("token", info.token);
    const ws = this.createSocket(url.toString());
    this._ws = ws;
    ws.onopen = () => {
      if (resume && this._resumeFrom) {
        this._send({ type: "session.resume", session_id: this._resumeFrom });
      } else {
        this._send({ type: "session.update", session: this._session });
      }
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(typeof ev.data === "string" ? ev.data : String(ev.data)); } catch { return; }
      this.handleMessage(msg);
    };
    ws.onerror = () => { /* onclose follows and carries the recovery logic */ };
    ws.onclose = () => this._onClose(ws);
  }

  _setPhase(phase) {
    if (this.phase === phase) return;
    this.phase = phase;
    this.emit("phase", { phase });
  }

  end() {
    this._wantOpen = false;
    this._discardPending();
    if (this._ws && this._ws.readyState === 1) {
      this._send({ type: "session.end" }); // avoids the billable 30 s resume window
    }
    try { this._ws && this._ws.close(); } catch { /* already closed */ }
    this._resumeFrom = null;
    this.sessionId = null;
    this.userSpeaking = this.agentSpeaking = false;
    this._setPhase("closed");
  }

  _onClose(ws) {
    if (ws !== this._ws) return; // a superseded socket
    this._ws = null;
    if (!this._wantOpen || this.phase === "closed" || this.phase === "error") return;
    this._discardPending();
    this.userSpeaking = this.agentSpeaking = false;
    this.emit("agent.flush", {});
    this._reconnect();
  }

  _reconnect() {
    if (this._attempt >= this.maxReconnects) {
      return this._fail("The voice connection dropped and I couldn't get it back. Press the mic to try again.",
        "reconnect_failed", true);
    }
    const attempt = ++this._attempt;
    this.emit("reconnecting", { attempt });
    this._setPhase("reconnecting");
    const resume = !!this._resumeFrom && attempt === 1; // resume once, then start fresh
    this.setTimer(() => { if (this._wantOpen) this._connect(resume); }, Math.min(500 * attempt, 2000));
  }

  _fail(message, code, recoverable) {
    this._wantOpen = false;
    this._setPhase("error");
    this.emit("error", { message, code, recoverable });
  }

  // ---- sending ---------------------------------------------------------
  _send(obj) {
    if (this._ws && this._ws.readyState === 1) this._ws.send(JSON.stringify(obj));
  }

  /** @param {string} base64Pcm16 24 kHz mono PCM16 */
  sendAudio(base64Pcm16) {
    if (this.phase !== "ready" || this.muted) return false; // input.audio only after session.ready
    this._send({ type: "input.audio", audio: base64Pcm16 });
    return true;
  }

  setMuted(m) { this.muted = !!m; }

  /** Ask the agent to say something now (e.g. "the repainted scene is ready"). Only when idle. */
  nudge(instructions) {
    if (this.phase !== "ready") return false;
    if (this.userSpeaking || this.agentSpeaking || this.pending.length) {
      this._pendingNudge = instructions; // deliver when things go quiet (latest wins)
      return false;
    }
    this._send({ type: "reply.create", instructions });
    return true;
  }

  _flushNudge() {
    if (this._pendingNudge && !this.userSpeaking && !this.agentSpeaking && !this.pending.length) {
      const text = this._pendingNudge;
      this._pendingNudge = null;
      this._send({ type: "reply.create", instructions: text });
    }
  }

  // ---- receiving -------------------------------------------------------
  handleMessage(msg) {
    switch (msg.type) {
      case "session.ready":
        this.sessionId = msg.session_id || this.sessionId;
        this._resumeFrom = this.sessionId;
        this._attempt = 0;
        this._setPhase("ready");
        this.emit("ready", { sessionId: this.sessionId });
        break;
      case "session.updated":
        break;
      case "session.ended":
        this._resumeFrom = null;
        this.emit("ended", msg);
        if (this._wantOpen) { this._wantOpen = false; this._setPhase("closed"); }
        break;
      case "session.error":
      case "error":
        this._onServerError(msg);
        break;
      case "input.speech.started":
        this.userSpeaking = true;
        // barge-in: cut the agent's audio locally the moment the user talks over it
        if (this.agentSpeaking) { this._dropAudio = true; this.emit("agent.flush", { reason: "barge-in" }); }
        this.emit("speech", { speaking: true });
        break;
      case "input.speech.stopped":
        this.userSpeaking = false;
        this.emit("speech", { speaking: false });
        this._flushNudge();
        break;
      case "transcript.user.delta":
        this.emit("transcript.user.delta", { text: msg.text || "" });
        break;
      case "transcript.user":
        this.emit("transcript.user", { text: msg.text || "", itemId: msg.item_id });
        break;
      case "reply.started":
        this.agentSpeaking = true;
        this._dropAudio = false;
        this.emit("reply", { started: true, replyId: msg.reply_id });
        break;
      case "reply.audio":
        // chunks of an interrupted reply can still be in flight; never play them after a barge-in
        if (msg.data && !this._dropAudio) this.emit("agent.audio", { data: msg.data });
        break;
      case "transcript.agent":
        this.emit("transcript.agent", { text: msg.text || "", interrupted: !!msg.interrupted, replyId: msg.reply_id });
        break;
      case "tool.call":
        this._onToolCall(msg);
        break;
      case "reply.done":
        this._onReplyDone(msg);
        break;
      default:
        this.emit("unknown", msg);
    }
  }

  _onServerError(msg) {
    const code = msg.code || "error";
    const human = humanError(code, msg.message);
    if (FRESH_SESSION_CODES.has(code)) {
      // resume impossible: forget the old id and start a new session (story context is in the prompt)
      this._resumeFrom = null;
      this.emit("notice", { message: human });
      if (this._ws) { const w = this._ws; this._ws = null; try { w.close(); } catch { /* */ } }
      if (this._wantOpen) { this._attempt = Math.max(this._attempt, 1); this._reconnect(); }
      return;
    }
    if (code === "UNAUTHORIZED" || code === "FORBIDDEN" || code === "agent_init_failed" || code === "invalid_config") {
      const w = this._ws; this._ws = null; try { w && w.close(); } catch { /* */ }
      return this._fail(human, code, true);
    }
    this.emit("notice", { message: human, code });
  }

  _onToolCall(msg) {
    const { call_id: callId, name } = msg;
    let args = msg.arguments;
    if (typeof args === "string") { try { args = JSON.parse(args || "{}"); } catch { args = {}; } }
    this.emit("tool.started", { callId, name, args });
    const run = Promise.resolve()
      .then(() => this.executeTool(name, args || {}))
      .catch(() => ({ ok: false, error: "That action failed. Please try again." }))
      .then((result) => {
        this.emit("tool.finished", { callId, name, args, result });
        return { callId, result };
      });
    this.pending.push(run);
  }

  async _onReplyDone(msg) {
    this.agentSpeaking = false;
    this.emit("reply", { started: false, status: msg.status });
    const batch = this.pending;
    this.pending = [];
    if (msg.status === "interrupted") {
      this._dropAudio = true;
      this.emit("agent.flush", { reason: "interrupted" });
      // The agent will not receive these results; the actions already ran, so tell the UI.
      if (batch.length) Promise.all(batch).then((r) => this.emit("tool.dropped", { count: r.length }));
      this._flushNudge();
      return;
    }
    if (batch.length) {
      const done = await Promise.all(batch);
      for (const { callId, result } of done) {
        // result must be a JSON *string*, not a nested object
        this._send({ type: "tool.result", call_id: callId, result: JSON.stringify(result) });
      }
    }
    this._flushNudge();
  }

  _discardPending() {
    this.pending = [];
    this._pendingNudge = null;
  }
}

// ---- audio helpers (pure) -------------------------------------------------
export function floatTo16BitPCM(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

export function int16ToBase64(int16) {
  const bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

export function base64ToInt16(b64) {
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length - (bin.length % 2));
  const view = new Uint8Array(buf);
  for (let i = 0; i < view.length; i++) view[i] = bin.charCodeAt(i);
  return new Int16Array(buf);
}

/** Linear resample (used when the device can't give us 24 kHz directly). */
export function resample(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const pos = i * ratio, i0 = Math.floor(pos), i1 = Math.min(i0 + 1, input.length - 1), f = pos - i0;
    out[i] = input[i0] * (1 - f) + input[i1] * f;
  }
  return out;
}

// ---- UI state machine (pure) ----------------------------------------------
/**
 * Collapse voice + story + UI facts into one human-readable state.
 * States: idle listening speaking processing understanding generating editing
 *         ready rendering complete error
 */
export function deriveState({ voice = {}, story = null, toolsRunning = 0, lastTool = null, playing = false, error = null }) {
  if (error) return { state: "error", label: error };
  const scenes = story?.scenes || [];
  const generating = scenes.filter((s) => s.status === "generating");
  if (story?.status === "rendering") return { state: "rendering", label: "Rendering your illustrated story..." };
  if (voice.phase === "connecting") return { state: "processing", label: "Connecting to your storyteller..." };
  if (voice.phase === "reconnecting") return { state: "processing", label: "Reconnecting..." };
  if (voice.userSpeaking) return { state: "listening", label: "Listening..." };
  if (toolsRunning > 0) return { state: "understanding", label: "Understanding your story..." };
  if (generating.length) {
    const n = generating[0].order;
    const repaint = generating.some((s) => s.revision > 0) || lastTool === "regenerate_scene";
    let label = repaint ? `Repainting scene ${n}...` : `Creating scene ${n}...`;
    if (lastTool === "modify_character") label = "Keeping the character's look consistent in every scene...";
    else if (lastTool === "modify_story_style") label = "Restyling every scene...";
    return { state: repaint || lastTool === "modify_character" || lastTool === "modify_story_style" ? "editing" : "generating", label };
  }
  if (voice.agentSpeaking) return { state: "speaking", label: "NarraFlow is speaking..." };
  if (playing) return { state: "complete", label: "Playing your story" };
  if (story?.status === "complete") return { state: "complete", label: "Your illustrated story is ready" };
  if (voice.phase === "ready") return { state: "listening", label: "Listening. Tell me your story." };
  if (scenes.length && scenes.every((s) => s.status === "ready")) return { state: "ready", label: "Your story is ready to play" };
  return { state: "idle", label: "Press the microphone to begin" };
}
