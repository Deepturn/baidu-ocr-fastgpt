import asyncio
import base64
import json
import os
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image

from app.errors import ServiceError
from app.render import Renderer
from app.main import create_app
from conftest import multipart, pdf, png


async def test_pdf_resources_isolated_serial_and_closed(settings, tmp_path):
    source = tmp_path / "input"
    source.write_bytes(pdf(3))
    async with Renderer(source, settings) as worker:
        process = worker.process
        assert process.pid != os.getpid()
        assert worker.pages == 3
        for _ in range(worker.pages):
            form = await worker.next_page()
            assert not (tmp_path / "page.form").exists()
            values = parse_qs(form.decode())
            with Image.open(BytesIO(base64.b64decode(values["image"][0]))) as image:
                assert image.mode == "RGB"
                assert image.format == "JPEG"
    assert process.returncode is not None
    assert not (tmp_path / "page.form").exists()


async def test_worker_cancel_reaped_before_return(settings, tmp_path):
    source = tmp_path / "input"
    source.write_bytes(pdf(2))
    entered = asyncio.Event()
    process = None
    async def wait_forever():
        nonlocal process
        async with Renderer(source, settings) as worker:
            process = worker.process
            entered.set()
            await asyncio.sleep(10)
    task = asyncio.create_task(wait_forever())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)


async def test_deadline_during_sync_worker_render_is_responsive(settings, tmp_path, monkeypatch):
    import app.render as render
    source = tmp_path / "input"
    source.write_bytes(pdf(1))
    real_exec = asyncio.create_subprocess_exec
    children = []
    async def hung_worker(*args, **kwargs):
        process = await real_exec(
            args[0], "-c", "import time; time.sleep(60)",
            stdin=kwargs["stdin"], stdout=kwargs["stdout"], stderr=kwargs["stderr"],
        )
        children.append(process)
        return process
    monkeypatch.setattr(render.asyncio, "create_subprocess_exec", hung_worker)
    heartbeat = []
    async def tick():
        for _ in range(5):
            await asyncio.sleep(0.01)
            heartbeat.append(1)
    ticker = asyncio.create_task(tick())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            async with Renderer(source, settings):
                pytest.fail("hung worker must not initialize")
    await ticker
    assert len(heartbeat) == 5
    assert len(children) == 1
    assert children[0].returncode is not None


async def test_worker_process_crash_is_safe(settings, tmp_path):
    source = tmp_path / "input"
    source.write_bytes(png())
    async with Renderer(source, settings) as worker:
        worker.process.kill()
        await worker.process.wait()
        with pytest.raises(ServiceError, match="render_worker_failed"):
            await worker.next_page()


async def test_concurrent_documents_have_distinct_processes(settings, tmp_path):
    sources = [tmp_path / "a", tmp_path / "b"]
    for source in sources:
        source.mkdir()
        (source / "input").write_bytes(pdf(1))
    async with Renderer(sources[0] / "input", settings) as first:
        async with Renderer(sources[1] / "input", settings) as second:
            assert first.process.pid != second.process.pid
            result = await asyncio.gather(first.next_page(), second.next_page())
            assert len(result) == 2


async def test_rgb_alpha_and_pixel_limits(settings, tmp_path):
    source = tmp_path / "input"
    source.write_bytes(png(mode="RGBA"))
    async with Renderer(source, settings) as worker:
        body = await worker.next_page()
    with Image.open(BytesIO(base64.b64decode(parse_qs(body.decode())["image"][0]))) as result:
        assert result.mode == "RGB"
    small = replace(settings, max_source_pixels=100)
    async with Renderer(source, small) as worker:
        with pytest.raises(ServiceError):
            await worker.next_page()


async def test_asgi_disconnect_reaps_real_worker_and_cleans_temp(settings, monkeypatch):
    import app.main as main

    workers = []
    upstream_started = asyncio.Event()
    upstream_cancelled = asyncio.Event()

    class TrackedRenderer(Renderer):
        async def __aenter__(self):
            result = await super().__aenter__()
            workers.append(self)
            return result

    async def upstream(request):
        if request.url.path.endswith("token"):
            return httpx.Response(200, json={
                "access_token": "dummy-access-token", "expires_in": 3600,
            })
        upstream_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            upstream_cancelled.set()
            raise

    monkeypatch.setattr(main, "Renderer", TrackedRenderer)
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    body = multipart(pdf(2))
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Disconnect only once a real worker has rendered and an OCR call is active.
        await upstream_started.wait()
        return {"type": "http.disconnect"}

    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/v2/parse/file",
        "raw_path": b"/v2/parse/file", "query_string": b"", "root_path": "",
        "headers": [
            (b"authorization", ("Bearer " + settings.inbound_token).encode()),
            (b"content-type", b"multipart/form-data; boundary=unit-boundary"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 12345), "server": ("test", 80),
    }
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(5):
            await app(scope, receive, send)
        assert app.state.active_files == 0
        assert upstream_cancelled.is_set()
        assert len(workers) == 1
        worker = workers[0]
        assert worker.process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(worker.process.pid, 0)
        assert not worker.source.parent.exists()
    assert messages[0]["status"] == 400
    payload = json.loads(b"".join(message.get("body", b"") for message in messages))
    assert payload["error"] == "client_disconnected"
    assert payload["markdown"] == ""
    assert payload["pages"] == 0
