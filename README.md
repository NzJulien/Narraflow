# NarraFlow

Real-time multi-agent storytelling: **Director → Writer → Memory → Artist →
Cinematographer**, streamed live to the browser over SSE.

⚠️ **About your Fireworks key:** you pasted `fw_2kMDsWjRV7CSxrxLKR9c8C` in
plain text in our chat. Treat that key as compromised — rotate/revoke it in
the [Fireworks dashboard](https://fireworks.ai) and generate a fresh one.
Put the new key in `backend/.env` (copied from `backend/.env.example`,
which is git-ignored) — never in code, chat, or version control.

## Run it

```bash
cd narraflow/backend
cp .env.example .env        # then edit .env with your real key + backends
pip install -r requirements.txt --break-system-packages
uvicorn app.main:app --reload
# open http://localhost:8000/app/
```

Or with Docker:

```bash
cd narraflow
docker build -t narraflow -f backend/Dockerfile .
docker run --env-file backend/.env -p 8000:8000 narraflow
```

With no `.env` at all, every agent defaults to `mock` and the demo still
runs end-to-end offline (no images, Ken Burns motion only, deterministic
prose) — this is the fallback the frontend also drops into automatically
if it can't reach a backend.

## What's in the folder

```
narraflow/
  backend/
    app/
      main.py            FastAPI routes + SSE endpoint (unchanged)
      orchestrator.py     Pipeline coordinator (unchanged, + token streaming)
      director.py         Plan agent  (reconstructed - see note below)
      writer.py            Prose agent (reconstructed + write_scene_stream)
      memory.py            World-state agent (reconstructed)
      artist.py            Image agent (reconstructed)
      cinematographer.py  Motion/video agent (unchanged)
      voice.py             Speech-to-text fallback (unchanged)
    requirements.txt, Dockerfile, .env.example
  frontend/
    index.html            Full demo UI (unchanged, + streaming-text edits)
  README.md
```

**Note on director.py / writer.py / memory.py / artist.py:** only
`cinematographer.py`, `voice.py`, `main.py`, and `orchestrator.py` were in
the code you pasted — the other four agents were imported but not
included. I reconstructed them to match the exact function signatures
your existing code already calls (`plan_story`, `write_scene`,
`get_world`/`new_world`/`update_memory`, `generate_image`), following the
same mock/fireworks/defensive pattern as the modules you did provide. If
you have your real versions, drop them in — nothing else changes.

## What changed vs. what you already had

Almost everything on your checklist was **already built** in the code you
pasted: the pipeline timeline, per-agent latency + total time, GPU/backend
badges, execution log, memory diffs + relationship graph, skeleton
loaders, Ken Burns motion on arrival, progress bar, mobile/accessibility
basics, per-scene image retry, copy/Markdown/PDF/image export, TTS
playback, and configurable scenes/genre/tone/image style/camera style. I
left all of that as-is.

Genuinely new, additive-only (no existing behavior removed or rewritten):

- **Token-level streaming** — `writer.write_scene_stream()` +
  `scene_token` SSE events, so prose now visibly "types" into each scene
  as it's generated instead of appearing all at once. The final `scene`
  event still lands afterward with the canonical structured scene, so
  nothing downstream changed.
- **Configurable scene length** (short/medium/long) — new dropdown,
  threaded through the same `_apply_style_hints` pattern already used for
  genre/tone.
- **Missing agent modules** reconstructed (see note above) so the project
  actually runs.

## Judging-checklist mapping (quick reference)

| Ask | Where |
|---|---|
| Pipeline visible, in order | `.pipeline` timeline in `index.html`, driven by `agent_start`/`timing` events |
| Streaming tokens | `scene_token` events (new) |
| Per-agent latency + total time | `timing` events, `.stat` bar, `elapsed` |
| GPU/backend badges | `/` status endpoint → `.badge-row` |
| Execution log | `#logpanel` |
| Memory visibly persists | `memory.py` + `memory_diff` events + world graph |
| Skeletons / no blank waits | `renderSceneSkeleton`, `.skeleton-*` |
| Ken Burns on image arrival | `renderMotion()` |
| Progress % | `.progress-fill` |
| Retry without full restart | `POST /scene/regenerate-image` |
| Export (copy/MD/PDF/images) | `.actions` buttons |
| TTS | `toggleTTS()` (Web Speech API) |
| Configurable scenes/genre/tone/length/image style/camera style | `.config-grid` |
| Graceful fallback everywhere | every agent module + `runMock()` in the frontend |
