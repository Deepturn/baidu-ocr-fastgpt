import asyncio
import logging
import ssl
import time
from dataclasses import replace

import httpx
import pytest

from app.baidu import BaiduClient
from app.errors import ServiceError
from conftest import Upstream


async def test_token_cache_and_expiry(settings):
    upstream = Upstream()
    client = BaiduClient(settings, transport=httpx.MockTransport(upstream))
    try:
        first = await client.get_token()
        assert await client.get_token() == first
        assert upstream.auth_calls == 1
        assert time.monotonic() + 3500 < client._refresh_at < time.monotonic() + 3600
        client._refresh_at = 0
        assert await client.get_token() != first
        assert upstream.auth_calls == 2
    finally:
        await client.close()
    assert client._token == ""


async def test_concurrent_token_and_rejected_refresh_no_stampede(settings):
    upstream = Upstream()
    client = BaiduClient(settings, transport=httpx.MockTransport(upstream))
    try:
        tokens = await asyncio.gather(*[client.get_token() for _ in range(10)])
        assert len(set(tokens)) == 1
        assert upstream.auth_calls == 1
        refreshed = await asyncio.gather(*[client.get_token(rejected=tokens[0]) for _ in range(10)])
        assert len(set(refreshed)) == 1
        assert refreshed[0] != tokens[0]
        assert upstream.auth_calls == 2
    finally:
        await client.close()


@pytest.mark.parametrize("code", [110, 111, "110", "111"])
async def test_token_rejection_refresh_once(settings, code):
    upstream = Upstream([{"error_code": code}, {"error_code": code}])
    client = BaiduClient(settings, transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(ServiceError, match="upstream_token_rejected"):
            await client.recognize(b"image=dummy")
        assert upstream.auth_calls == 2
        assert upstream.ocr_calls == 2
    finally:
        await client.close()


@pytest.mark.parametrize("result", [
    {"error_code": 18}, {"error_code": 282000},
    httpx.Response(429), httpx.Response(500),
    httpx.ReadTimeout("secret URL and token must not leak"),
])
async def test_transient_retry_once_and_rate(settings, result):
    upstream = Upstream([result, result])
    config = replace(settings, requests_per_second=20)
    client = BaiduClient(config, transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(ServiceError, match="upstream_unavailable"):
            await client.recognize(b"image=dummy")
        assert upstream.ocr_calls == 2
        assert upstream.times[1] - upstream.times[0] >= 0.045
    finally:
        await client.close()


@pytest.mark.parametrize("result", [
    {"error_code": 17}, {"words_result": []},
    {"words_result_num": True, "words_result": []},
    {"words_result_num": 2, "words_result": [{"words": "one"}]},
    {"words_result_num": 1, "words_result": [{"words": 1}]},
    httpx.Response(200, content=b"not-json"),
    httpx.Response(302, headers={"location": "https://untrusted.example/?secret"}),
])
async def test_schema_and_business_error_do_not_retry(settings, result):
    upstream = Upstream([result])
    client = BaiduClient(settings, transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(ServiceError):
            await client.recognize(b"image=dummy")
        assert upstream.ocr_calls == 1
    finally:
        await client.close()


@pytest.mark.parametrize("result", [
    {"error": "invalid_client", "error_description": "secret"},
    {"access_token": "token", "expires_in": 0},
    {"access_token": "token", "expires_in": True},
    {"access_token": "token\n", "expires_in": 3600},
])
async def test_bad_auth_response(settings, result):
    def handler(request):
        return httpx.Response(200, json=result)
    client = BaiduClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ServiceError, match="upstream_auth_failed"):
            await client.get_token()
        assert not client._token
    finally:
        await client.close()


async def test_auth_transient_bounded(settings):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    client = BaiduClient(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ServiceError, match="upstream_auth_unavailable"):
            await client.get_token()
        assert len(calls) == 2
    finally:
        await client.close()


async def test_upstream_timeout_total_deadline_and_cancel(settings):
    upstream = Upstream(delay=5)
    config = replace(settings, upstream_timeout_seconds=0.03)
    client = BaiduClient(config, transport=httpx.MockTransport(upstream))
    start = time.monotonic()
    try:
        with pytest.raises(ServiceError, match="upstream_unavailable"):
            await client.recognize(b"image=dummy")
        assert time.monotonic() - start < 0.5
        assert upstream.active == 0
        assert upstream.ocr_calls == 2
        async with asyncio.timeout(0.04):
            with pytest.raises(asyncio.CancelledError):
                await client.recognize(b"image=dummy")
        assert upstream.active == 0
    finally:
        await client.close()


async def test_response_and_form_bounds(settings):
    config = replace(settings, max_upstream_response_bytes=50)
    upstream = Upstream()
    client = BaiduClient(config, transport=httpx.MockTransport(upstream))
    try:
        with pytest.raises(ServiceError, match="encoded_page_too_large"):
            await client.recognize(b"x" * 10_000_000)
        assert upstream.auth_calls == 0
        with pytest.raises(ServiceError, match="upstream_response_too_large"):
            await client.get_token()
    finally:
        await client.close()


async def test_tls_redirect_and_logging_defaults(settings):
    client = BaiduClient(settings)
    try:
        assert client.http.follow_redirects is False
        context = client.http._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert logging.getLogger("httpx").disabled
        assert logging.getLogger("httpcore.http11").disabled
    finally:
        await client.close()
