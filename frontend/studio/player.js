// Illustrated story playback: scene timeline, captions, narration, controls.
// Time and speech are injected so the logic is testable without a browser.
import { Emitter } from "./protocol.js";

const SPEECH_GRACE_SECONDS = 6; // never wait longer than this past a scene's duration for narration to finish

export function playableScenes(story) {
  return (story?.scenes || []).filter((s) => s.status === "ready" && s.image_url);
}

export function buildTimeline(scenes) {
  let t = 0;
  const items = scenes.map((scene, index) => {
    const item = { index, scene, start: t, end: t + scene.duration };
    t += scene.duration;
    return item;
  });
  return { items, total: t };
}

export class StoryPlayer extends Emitter {
  constructor({ speak, cancelSpeak, now = () => performance.now(), setInterval: si = setInterval, clearInterval: ci = clearInterval } = {}) {
    super();
    this.speak = speak || ((_t, done) => done());
    this.cancelSpeak = cancelSpeak || (() => {});
    this.now = now;
    // Wrap, don't store: browsers throw "Illegal invocation" when setInterval is called with this === player.
    this._si = (fn, ms) => si(fn, ms);
    this._ci = (id) => ci(id);
    this.scenes = []; this.index = 0; this.playing = false; this.narration = true;
    this._sceneStart = 0; this._pausedAt = 0; this._speechDone = true; this._timer = null; this._token = 0;
  }

  load(scenes) {
    const currentId = this.scenes[this.index]?.id;
    this.scenes = scenes;
    const kept = currentId ? scenes.findIndex((s) => s.id === currentId) : -1;
    this.index = kept >= 0 ? kept : Math.min(this.index, Math.max(0, scenes.length - 1));
  }

  get total() { return buildTimeline(this.scenes).total; }

  elapsed() {
    const tl = buildTimeline(this.scenes);
    const inScene = this.playing ? (this.now() - this._sceneStart) / 1000 : (this._pausedAt - this._sceneStart) / 1000;
    const item = tl.items[this.index];
    return item ? Math.min(item.end, item.start + Math.max(0, inScene)) : 0;
  }

  play(from = 0) {
    if (!this.scenes.length) return false;
    this.index = Math.max(0, Math.min(this.scenes.length - 1, from));
    this.playing = true;
    this._enter();
    this._timer = this._timer || this._si(() => this._tick(), 100);
    this.emit("state", { playing: true });
    return true;
  }

  _enter() {
    this._token++;
    const token = this._token;
    this._sceneStart = this.now();
    const scene = this.scenes[this.index];
    this._speechDone = true;
    this.cancelSpeak();
    if (this.narration && (scene.narration || scene.summary)) {
      this._speechDone = false;
      this.speak(scene.narration || scene.summary, () => { if (token === this._token) this._speechDone = true; });
    }
    this.emit("scene", { index: this.index, scene, total: this.scenes.length });
  }

  _tick() {
    if (!this.playing) return;
    const scene = this.scenes[this.index];
    const t = (this.now() - this._sceneStart) / 1000;
    this.emit("progress", { elapsed: this.elapsed(), total: this.total, index: this.index });
    const timeUp = t >= scene.duration;
    const speechOk = this._speechDone || t >= scene.duration + SPEECH_GRACE_SECONDS;
    if (timeUp && speechOk) this._advance();
  }

  _advance() {
    if (this.index >= this.scenes.length - 1) return this.stop(true);
    this.index++;
    this._enter();
  }

  next() { if (this.index < this.scenes.length - 1) { this.index++; this._enter(); this._afterSeek(); } }
  prev() { if (this.index > 0) { this.index--; this._enter(); this._afterSeek(); } }
  replay() { this.play(0); }
  seek(i) { this.index = Math.max(0, Math.min(this.scenes.length - 1, i)); this._enter(); this._afterSeek(); }

  _afterSeek() { // navigating while paused shows the scene but stays paused
    if (!this.playing) { this._pausedAt = this.now(); this.cancelSpeak(); }
  }

  pause() {
    if (!this.playing) return;
    this.playing = false; this._pausedAt = this.now();
    this.cancelSpeak(); this._token++;
    this.emit("state", { playing: false });
  }

  resume() {
    if (this.playing || !this.scenes.length) return;
    this.playing = true;
    this._sceneStart = this.now() - (this._pausedAt - this._sceneStart);
    this._speechDone = true; // don't re-read half a sentence; the timer finishes the scene
    this.emit("state", { playing: true });
  }

  toggle() { this.playing ? this.pause() : (this._timer ? this.resume() : this.play(this.index)); }

  setNarration(on) { this.narration = !!on; if (!on) this.cancelSpeak(); }

  stop(finished = false) {
    this.playing = false;
    this.cancelSpeak(); this._token++;
    if (this._timer) { this._ci(this._timer); this._timer = null; }
    this.emit("state", { playing: false });
    if (finished) this.emit("end", {});
  }
}
