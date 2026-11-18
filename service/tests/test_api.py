import asyncio
import logging
import tempfile
from pathlib import Path

import httpx
import pytest

from conftest import Upstream, multipart, pdf, png


def assert_failure(response, status=200):
    assert response.status_code == status
    value = response.json()
    assert value["success"] is False
    assert value["markdown"] == ""
    assert value["pages"] == 0
    assert value["error"]
    assert value["statusText"] in {
        "pdfEncrypted", "pdfCorrupted", "invalidParseFile",
        "unsupportedParseFileType", "pdfParseFailed",
    }
    return value


async def test_auth_before_any_receive_and_health(api):
    async with api() as (client, upstream, app):
        received = False
        async def receive():
            nonlocal received
            received = True
            raise AssertionError("unauthorized request body was read")
        messages = []
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/v2/parse/file",
                 "raw_path": b"/v2/parse/file", "query_string": b"",
                 "root_path": "", "headers": [(b"authorization", b"Bearer wrong")],
                 "client": ("127.0.0.1", 1), "server": ("test", 80)}
        await app(scope, receive, send)
        assert messages[0]["status"] == 401
        assert not received
        response = await client.get("/healthz", headers={"Authorization": ""})
        assert response.json() == {"status": "ok"}
        assert upstream.auth_calls == upstream.ocr_calls == 0


@pytest.mark.parametrize("data,pages", [(png(), 1), (pdf(), 3)])
async def test_magic_not_filename_and_pages(api, data, pages):
    async with api() as (client, upstream, _):
        response = await client.post("/v2/parse/file",
                                     files={"file": ("not-an-image.txt", data, "text/plain")})
        assert response.status_code == 200
        value = response.json()
        assert value == {"success": True, "markdown": "\n\n".join(
            f"page {n}" for n in range(1, pages + 1)), "pages": pages, "error": ""}
        assert upstream.ocr_calls == pages
        assert upstream.auth_calls == 1
        assert all(form["multidirectional_recognize"] == ["true"] for form in upstream.forms)


async def test_middle_page_failure_no_partial_text(api):
    upstream = Upstream([
        {"words_result_num": 1, "words_result": [{"words": "first page secret"}]},
        {"error_code": 17, "error_msg": "credential and content must not leak"},
    ])
    async with api(upstream=upstream) as (client, _, __):
        response = await client.post("/v2/parse/file", files={"file": ("x.pdf", pdf())})
        value = assert_failure(response)
        assert value["error"] == "page_2:upstream_ocr_failed"
        assert upstream.ocr_calls == 2
        assert "secret" not in response.text


async def test_blank_pages_count_and_all_blank_fails(api):
    blank = {"words_result_num": 0, "words_result": []}
    async with api(upstream=Upstream([blank])) as (client, _, __):
        response = await client.post("/v2/parse/file", files={"file": ("x.pdf", pdf(2))})
        assert response.json()["pages"] == 2
        assert response.json()["markdown"].endswith("page 2")
    async with api(upstream=Upstream([blank] * 3)) as (client, _, __):
        response = await client.post("/v2/parse/file", files={"file": ("x.pdf", pdf(3))})
        assert assert_failure(response)["error"] == "no_text_recognized"


@pytest.mark.parametrize("data,status", [
    (b"%PDF-1.7 broken", "pdfCorrupted"),
    (pdf(password="protected"), "pdfEncrypted"),
    (pdf(password=""), "pdfEncrypted"),
    (b"GIF89a arbitrary content", "unsupportedParseFileType"),
    (b"\x89PNG\r\n\x1a\ncorrupted", "invalidParseFile"),
])
async def test_invalid_documents(api, data, status):
    async with api() as (client, upstream, _):
        response = await client.post("/v2/parse/file", files={"file": ("fake.pdf", data)})
        assert assert_failure(response)["statusText"] == status
        assert upstream.ocr_calls == 0


async def test_page_and_pixel_limit(api):
    async with api(max_pages=2) as (client, upstream, _):
        response = await client.post("/v2/parse/file", files={"file": ("x.pdf", pdf(3))})
        assert assert_failure(response)["error"] == "pdf_page_limit_exceeded"
        assert upstream.auth_calls == 0
    async with api(max_source_pixels=5000) as (client, upstream, _):
        response = await client.post("/v2/parse/file", files={"file": ("x.png", png())})
        assert_failure(response)
        assert upstream.auth_calls == 0


async def test_extra_fields_and_multiple_files(api):
    async with api() as (client, _, __):
        response = await client.post("/v2/parse/file", data={"mode": "ignored"},
                                     files={"file": ("x.png", png())})
        assert response.json()["success"]
        response = await client.post("/v2/parse/file", files=[
            ("file", ("x.png", png())), ("other", ("x.png", png())),
        ])
        assert_failure(response, 400)
        response = await client.post("/v2/parse/file", files={"other": ("x.png", png())})
        assert_failure(response, 400)
        response = await client.post("/v2/parse/file", files={"file": ("x.png", b"")})
        assert_failure(response, 400)


@pytest.mark.parametrize("body,content_type", [
    (b"no boundary", "application/octet-stream"),
    (b"--unit-boundary--\r\n", "multipart/form-data; boundary=unit-boundary"),
    (multipart(png(), ending=False), "multipart/form-data; boundary=unit-boundary"),
    (multipart(png()), "multipart/form-data"),
    (b"--unit-boundary\r\nBAD HEADER\r\n\r\nx", "multipart/form-data; boundary=unit-boundary"),
])
async def test_malformed_or_missing_multipart(api, body, content_type):
    async with api() as (client, upstream, _):
        response = await client.post("/v2/parse/file", content=body,
                                     headers={"Content-Type": content_type})
        assert_failure(response, 400)
        assert upstream.ocr_calls == 0


async def test_streamed_size_and_lie_content_length(api):
    async with api(max_file_mb=1, multipart_overhead_bytes=256) as (client, upstream, _):
        async def streamed():
            yield multipart(b"x" * (1024 * 1024 + 257))
        response = await client.post("/v2/parse/file", content=streamed(), headers={
            "Content-Type": "multipart/form-data; boundary=unit-boundary",
        })
        assert assert_failure(response, 413)["error"] == "body_too_large"
        body = multipart(png())
        response = await client.post("/v2/parse/file", content=body, headers={
            "Content-Type": "multipart/form-data; boundary=unit-boundary", "Content-Length": "1",
        })
        assert_failure(response, 400)
        assert upstream.auth_calls == 0


async def test_file_size_limit_during_chunked_upload(api):
    async with api(max_file_mb=1) as (client, _, __):
        data = multipart(b"x" * (1024 * 1024 + 1))
        async def streamed():
            for offset in range(0, len(data), 32768):
                yield data[offset:offset + 32768]
        response = await client.post("/v2/parse/file", content=streamed(), headers={
            "Content-Type": "multipart/form-data; boundary=unit-boundary",
        })
        assert assert_failure(response, 413)["error"] == "file_too_large"


async def test_busy_rejected_before_receive(api):
    started = asyncio.Event()
    finish = asyncio.Event()
    async def slow_body():
        started.set()
        await finish.wait()
        yield multipart(png())
    async with api(max_concurrent_files=1) as (client, _, app):
        first = asyncio.create_task(client.post("/v2/parse/file", content=slow_body(), headers={
            "Content-Type": "multipart/form-data; boundary=unit-boundary",
        }))
        await started.wait()
        read_second = False
        async def second_body():
            nonlocal read_second
            read_second = True
            yield b"not read"
        response = await client.post("/v2/parse/file", content=second_body())
        assert_failure(response, 429)
        assert not read_second
        assert app.state.active_files == 1
        finish.set()
        assert (await first).json()["success"]
        assert app.state.active_files == 0


async def test_global_ocr_rate_and_concurrency(api):
    upstream = Upstream(delay=0.03)
    async with api(upstream=upstream, requests_per_second=20) as (client, _, __):
        responses = await asyncio.gather(*[
            client.post("/v2/parse/file", files={"file": ("x.pdf", pdf(2))}) for _ in range(2)
        ])
        assert all(response.json()["success"] for response in responses)
        assert upstream.maximum == 1
        assert all(b - a >= 0.045 for a, b in zip(upstream.times, upstream.times[1:]))


async def test_cleanup_success_failure_timeout(api, monkeypatch):
    import app.main as main
    created = []
    original = tempfile.TemporaryDirectory
    def track(*args, **kwargs):
        directory = original(*args, **kwargs)
        created.append(Path(directory.name))
        return directory
    monkeypatch.setattr(main.tempfile, "TemporaryDirectory", track)
    async with api() as (client, _, __):
        await client.post("/v2/parse/file", files={"file": ("x.png", png())})
        await client.post("/v2/parse/file", files={"file": ("x.pdf", b"%PDF-bad")})
    async with api(total_timeout_seconds=0.05) as (client, _, app):
        async def blocked():
            await asyncio.sleep(5)
            yield multipart(png())
        response = await client.post("/v2/parse/file", content=blocked(), headers={
            "Content-Type": "multipart/form-data; boundary=unit-boundary",
        })
        assert assert_failure(response)["error"] == "total_timeout"
        assert app.state.active_files == 0
    assert len(created) == 3
    assert all(path.parent == Path("/tmp") for path in created)
    assert all(not path.exists() for path in created)


async def test_logs_no_names_content_tokens_or_upstream_urls(api, settings, caplog):
    caplog.set_level(logging.DEBUG)
    upstream = Upstream([{"error_code": 17, "error_msg": "sensitive-body-marker"}])
    async with api(upstream=upstream) as (client, _, __):
        response = await client.post("/v2/parse/file",
                                     files={"file": ("sensitive-file-name.png", png())})
        assert_failure(response)
    log = caplog.text
    for secret in (settings.api_key, settings.secret_key, settings.inbound_token,
                   "dummy-access-token", "access_token", "sensitive-file-name",
                   "sensitive-body-marker", "aip.baidubce.com"):
        assert secret not in log
