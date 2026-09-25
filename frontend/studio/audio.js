// Browser audio for the voice agent: microphone capture -> 24 kHz PCM16 base64,
// and gap-free, instantly-interruptible playback of the agent's reply audio.
import { floatTo16BitPCM, int16ToBase64, base64ToInt16, resample } from "./protocol.js";

export const SAMPLE_RATE = 24000;
const CHUNK_MS = 50;

const WORKLET_SRC = `
class PCMCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("pcm-capture", PCMCapture);`;

export class MicError extends Error {
  constructor(message, code) { super(message); this.code = code; }
}

export function explainMicError(err) {
  const name = err && err.name;
  if (name === "NotAllowedError" || name === "SecurityError")
    return new MicError("Microphone access is blocked. Allow the microphone for this site in your browser settings, then press the mic again.", "denied");
  if (name === "NotFoundError" || name === "OverconstrainedError")
    return new MicError("I can't find a microphone. Plug one in (or pick one in your system settings) and try again.", "none");
  if (name === "NotReadableError" || name === "AbortError")
    return new MicError("Your microphone is being used by another app. Close it and try again.", "busy");
  return new MicError("I couldn't start the microphone. Please try again.", "unknown");
}

export function checkSupport() {
  if (!window.isSecureContext)
    return "Voice needs a secure connection (https, or localhost). Open the site over https.";
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)
    return "This browser can't use the microphone. Try a recent Chrome, Edge, Firefox or Safari.";
  if (!window.AudioContext && !window.webkitAudioContext) return "This browser doesn't support Web Audio.";
  if (!window.AudioWorkletNode) return "This browser is too old for live audio. Try a recent Chrome, Edge, Firefox or Safari.";
  if (typeof WebSocket === "undefined") return "This browser doesn't support WebSockets.";
  return null;
}

/** One AudioContext shared by capture and playback (echo cancellation works best that way). */
export function createContext() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  try { return new Ctx({ sampleRate: SAMPLE_RATE, latencyHint: "interactive" }); }
  catch { return new Ctx({ latencyHint: "interactive" }); } // some browsers refuse a fixed rate: we resample instead
}

export class MicCapture {
  constructor(ctx, { onChunk, onLevel }) {
    this.ctx = ctx; this.onChunk = onChunk; this.onLevel = onLevel;
    this.stream = null; this.node = null; this.source = null; this._buf = []; this._n = 0; this._url = null;
  }

  async start() {
    try {
      // Echo cancellation lets the agent talk through speakers without hearing itself.
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: true, channelCount: 1 },
      });
    } catch (err) { throw explainMicError(err); }
    try {
      if (this.ctx.state === "suspended") await this.ctx.resume();
      this._url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
      await this.ctx.audioWorklet.addModule(this._url);
      this.source = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, "pcm-capture");
      const need = Math.round(this.ctx.sampleRate * CHUNK_MS / 1000);
      this.node.port.onmessage = (e) => {
        this._buf.push(e.data); this._n += e.data.length;
        if (this._n < need) return;
        const merged = new Float32Array(this._n);
        let o = 0; for (const a of this._buf) { merged.set(a, o); o += a.length; }
        this._buf = []; this._n = 0;
        let sum = 0; for (let i = 0; i < merged.length; i += 8) sum += merged[i] * merged[i];
        this.onLevel && this.onLevel(Math.sqrt(sum / (merged.length / 8)));
        const at24k = resample(merged, this.ctx.sampleRate, SAMPLE_RATE);
        this.onChunk(int16ToBase64(floatTo16BitPCM(at24k)));
      };
      this.source.connect(this.node); // not connected to the destination: no mic monitoring
    } catch {
      this.stop();
      throw new MicError("Live audio couldn't start in this browser. Try a recent Chrome, Edge, Firefox or Safari.", "worklet");
    }
  }

  stop() {
    try { this.node && (this.node.port.onmessage = null, this.node.disconnect()); } catch { /* */ }
    try { this.source && this.source.disconnect(); } catch { /* */ }
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this._url) URL.revokeObjectURL(this._url);
    this.stream = this.node = this.source = this._url = null; this._buf = []; this._n = 0;
  }
}

export class AgentPlayer {
  constructor(ctx, { onSpeaking } = {}) {
    this.ctx = ctx; this.onSpeaking = onSpeaking || (() => {});
    this.gain = ctx.createGain();
    this.analyser = ctx.createAnalyser(); this.analyser.fftSize = 256;
    this.gain.connect(this.analyser); this.analyser.connect(ctx.destination);
    this.sources = new Set(); this.next = 0; this.speaking = false;
  }

  enqueue(base64) {
    const pcm = base64ToInt16(base64);
    if (!pcm.length) return;
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const buf = this.ctx.createBuffer(1, f32.length, SAMPLE_RATE);
    buf.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf; src.connect(this.gain);
    this.next = Math.max(this.next, this.ctx.currentTime + 0.04);
    src.start(this.next); this.next += buf.duration;
    this.sources.add(src);
    src.onended = () => { this.sources.delete(src); if (!this.sources.size) this._set(false); };
    this._set(true);
  }

  /** Cut everything that's queued right now (barge-in / interruption). */
  flush() {
    for (const s of this.sources) { s.onended = null; try { s.stop(); } catch { /* */ } }
    this.sources.clear(); this.next = 0; this._set(false);
  }

  _set(v) { if (this.speaking !== v) { this.speaking = v; this.onSpeaking(v); } }

  level() {
    const d = new Uint8Array(this.analyser.fftSize);
    this.analyser.getByteTimeDomainData(d);
    let s = 0; for (const v of d) { const x = (v - 128) / 128; s += x * x; }
    return Math.sqrt(s / d.length);
  }
}
