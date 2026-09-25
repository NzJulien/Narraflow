import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Isolated, offline defaults for every test run.
os.environ.setdefault("NARRAFLOW_DATA_DIR", tempfile.mkdtemp(prefix="nf-test-"))
os.environ["IMAGE_PROVIDER"] = "illustrator"
os.environ.pop("ASSEMBLYAI_API_KEY", None)
os.environ.pop("ASSEMBLYAI_BASE_URL", None)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRAFLOW_DATA_DIR", str(tmp_path))
    from app.studio import images

    images.get_provider(refresh=True)
    return tmp_path


@pytest.fixture()
def client(data_dir):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def story_id(data_dir):
    from app.studio import store
    from app.studio.models import Story

    return store.save(Story()).id


@pytest.fixture()
def live_server(data_dir):
    """A real uvicorn server on a free port (for streaming endpoints TestClient can't drive)."""
    import socket
    import threading
    import time

    import uvicorn
    from app.main import app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
