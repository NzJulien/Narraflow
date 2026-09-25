"""A local stand-in for AssemblyAI's Voice Agent API, for end-to-end tests.

Implements the documented protocol closely enough to drive the real browser
client: GET /v1/token (Bearer auth, single-use tokens) and the /v1/ws WebSocket
(session.update -> session.ready, input.audio, speech events, transcripts,
tool.call / tool.result, reply.audio / reply.done, session.resume).

It records every client message so tests can assert on exactly what the browser
sent. It does NOT prove behaviour against the real service; it proves the
client speaks the documented protocol.
"""
import asyncio
import base64
import json
import math
import struct
import threading
import uuid
from http import HTTPStatus

import websockets
from websockets.asyncio.server import serve

API_KEY = "fake-assemblyai-key"


def tone(seconds=0.4, freq=440, rate=24000):
    n = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))
    return base64.b64encode(pcm).decode()


STORY_ARGS = {
    "narration_text": "Once upon a time, Amara lived in a village under the mountains.",
    "characters": [{"name": "Amara", "description": "a curious young girl", "age": "about 9",
                    "appearance": "dark skin, long black braids", "clothing": "blue dress, brown sandals"}],
    "locations": [{"name": "the village", "visual_traits": ["stone houses", "mountains behind"]}],
    "scenes": [{"narration": "Once upon a time, Amara lived in a village under the mountains.",
                "summary": "Amara's mountain village", "setting": "the village", "characters": ["Amara"],
                "emotion": "calm", "visual_prompt": "a small village under mountains at dusk"}],
}


class FakeAssemblyAI:
    def __init__(self, port=0):
        self.port = port
        self.tokens = set()
        self.used_tokens = set()
        self.sessions = {}
        self.log = []          # (conn_no, message dict)
        self.http_log = []     # (path, headers)
        self.conns = []
        self.mode = "story"    # story | long_reply | silent
        self.audio_chunks_before_speech = 8
        self._loop = None
        self._thread = None
        self._server = None
        self._ready = threading.Event()
        self._conn_no = 0

    # ---- lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "fake server failed to start"
        return f"http://127.0.0.1:{self.port}"

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        async def process_request(conn, request):
            if request.path.startswith("/v1/token"):
                self.http_log.append((request.path, {k.lower(): v for k, v in request.headers.items()}))
                if request.headers.get("Authorization") != f"Bearer {API_KEY}":
                    return conn.respond(HTTPStatus.UNAUTHORIZED, "bad key\n")
                tok = "tmp_" + uuid.uuid4().hex
                self.tokens.add(tok)
                return conn.respond(HTTPStatus.OK, json.dumps({"token": tok}) + "\n")
            return None

        self._server = await serve(self._handler, "127.0.0.1", self.port, process_request=process_request)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        await self._server.serve_forever()

    def stop(self):
        if not (self._loop and self._server):
            return

        async def shutdown():
            self._server.close()
            await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=5)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass

    # ---- helpers for tests
    def messages(self, type_=None):
        return [m for _, m in self.log if type_ is None or m.get("type") == type_]

    def drop_connections(self):
        for ws in list(self.conns):
            asyncio.run_coroutine_threadsafe(ws.close(), self._loop)

    def push(self, message):
        for ws in list(self.conns):
            asyncio.run_coroutine_threadsafe(ws.send(json.dumps(message)), self._loop)

    # ---- protocol
    async def _handler(self, ws):
        self._conn_no += 1
        conn_no = self._conn_no
        from urllib.parse import parse_qs, urlparse

        token = parse_qs(urlparse(ws.request.path).query).get("token", [""])[0]
        if token not in self.tokens or token in self.used_tokens:
            await ws.send(json.dumps({"type": "session.error", "code": "UNAUTHORIZED", "message": "invalid or used token"}))
            await ws.close()
            return
        self.used_tokens.add(token)
        self.conns.append(ws)
        state = {"audio": 0, "spoke": False, "session_id": None}
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.log.append((conn_no, msg))
                t = msg.get("type")
                if t == "session.update":
                    state["session_id"] = "sess_" + uuid.uuid4().hex[:8]
                    self.sessions[state["session_id"]] = msg["session"]
                    await ws.send(json.dumps({"type": "session.ready", "session_id": state["session_id"]}))
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif t == "session.resume":
                    sid = msg.get("session_id")
                    if sid in self.sessions:
                        state["session_id"] = sid
                        state["spoke"] = True  # don't replay the scripted story after a resume
                        await ws.send(json.dumps({"type": "session.ready", "session_id": sid}))
                    else:
                        await ws.send(json.dumps({"type": "session.error", "code": "session_not_found", "message": "gone"}))
                elif t == "input.audio":
                    state["audio"] += 1
                    if state["audio"] == self.audio_chunks_before_speech and not state["spoke"] and self.mode != "silent":
                        state["spoke"] = True
                        asyncio.create_task(self._script(ws))
                elif t == "tool.result":
                    self.tool_results = getattr(self, "tool_results", []) + [msg]
                    asyncio.create_task(self._followup(ws))
                elif t == "session.end":
                    await ws.send(json.dumps({"type": "session.ended", "session_duration_seconds": 1.0}))
        except websockets.ConnectionClosed:
            pass
        finally:
            if ws in self.conns:
                self.conns.remove(ws)

    async def _send(self, ws, obj, delay=0.0):
        if delay:
            await asyncio.sleep(delay)
        await ws.send(json.dumps(obj))

    async def _script(self, ws):
        await self._send(ws, {"type": "input.speech.started"})
        await self._send(ws, {"type": "transcript.user.delta", "text": "Once upon a time"}, 0.15)
        await self._send(ws, {"type": "input.speech.stopped"}, 0.15)
        await self._send(ws, {"type": "transcript.user", "text": STORY_ARGS["narration_text"], "item_id": "i1"}, 0.1)
        await self._send(ws, {"type": "reply.started", "reply_id": "r1"}, 0.1)
        await self._send(ws, {"type": "tool.call", "call_id": "call_1", "name": "add_story_content", "arguments": STORY_ARGS})
        if self.mode == "long_reply":
            for _ in range(20):  # ~6s of speech, streamed faster than realtime like the real service
                await self._send(ws, {"type": "reply.audio", "data": tone(0.3)}, 0.02)
            return  # test decides when to interrupt
        await self._send(ws, {"type": "reply.audio", "data": tone(0.4)})
        await self._send(ws, {"type": "transcript.agent", "text": "Lovely, painting that now.", "reply_id": "r1", "interrupted": False})
        await self._send(ws, {"type": "reply.done", "status": "completed"}, 0.3)

    async def _followup(self, ws):
        await self._send(ws, {"type": "reply.started", "reply_id": "r2"}, 0.1)
        await self._send(ws, {"type": "reply.audio", "data": tone(0.3, 660)})
        await self._send(ws, {"type": "transcript.agent", "text": "Tell me what happens next.", "reply_id": "r2", "interrupted": False})
        await self._send(ws, {"type": "reply.done", "status": "completed"}, 0.2)
