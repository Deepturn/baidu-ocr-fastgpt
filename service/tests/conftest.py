import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings():
    return Settings(
        api_key="dummy-api-key-not-real", secret_key="dummy-secret-not-real",
        inbound_token="dummy-inbound-token-00000000000000000000",
        requests_per_second=100, total_timeout_seconds=10, upstream_timeout_seconds=2,
    )


def png(size=(100, 100), mode="RGB"):
    with BytesIO() as output:
        with Image.new(mode, size, "white") as image:
            image.save(output, format="PNG", pnginfo=None)
        return output.getvalue()


def pdf(pages=3, password=None, size=(72, 144)):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(*size)
    if password is not None:
        writer.encrypt(password, owner_password="owner-secret", algorithm="RC4-128")
    with BytesIO() as output:
        writer.write(output)
        return output.getvalue()


def multipart(data: bytes, *, extra=b"", ending=True, name=b"file"):
    return (b"--unit-boundary\r\nContent-Disposition: form-data; name=\"" + name
            + b"\"; filename=\"sensitive-name.txt\"\r\nContent-Type: text/plain\r\n\r\n"
            + data + b"\r\n" + extra + (b"--unit-boundary--\r\n" if ending else b""))


class Upstream:
    def __init__(self, responses=None, *, delay=0):
        self.responses = list(responses or [])
        self.delay = delay
        self.auth_calls = 0
        self.ocr_calls = 0
        self.active = 0
        self.maximum = 0
        self.times = []
        self.forms = []
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if request.url.path.endswith("token"):
            self.auth_calls += 1
            return httpx.Response(200, json={
                "access_token": f"dummy-access-token-{self.auth_calls}", "expires_in": 3600,
            })
        self.ocr_calls += 1
        self.forms.append(parse_qs(request.content.decode("ascii")))
        self.times.append(time.monotonic())
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(self.delay)
            result = self.responses.pop(0) if self.responses else {
                "words_result_num": 1,
                "words_result": [{"words": f"page {self.ocr_calls}"}],
            }
            if isinstance(result, Exception):
                raise result
            if isinstance(result, httpx.Response):
                return result
            return httpx.Response(200, json=result)
        finally:
            self.active -= 1


@pytest.fixture
def api(settings):
    @asynccontextmanager
    async def factory(*, upstream=None, config=None, **overrides):
        config = config or replace(settings, **overrides)
        upstream = upstream or Upstream()
        app = create_app(config, transport=httpx.MockTransport(upstream))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer " + config.inbound_token},
            ) as client:
                yield client, upstream, app
    return factory
