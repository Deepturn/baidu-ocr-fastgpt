import asyncio
import json
import logging
import ssl
import time
from urllib.parse import urlencode

import httpx

from .config import Settings
from .encoding import form_fits
from .errors import ServiceError


class RateLimiter:
    def __init__(self, requests_per_second: float):
        self.interval = 1 / requests_per_second
        self.lock = asyncio.Lock()
        self.next_request = 0.0

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            await asyncio.sleep(max(0, self.next_request - now))
            self.next_request = time.monotonic() + self.interval


class BaiduClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        # httpx request logging includes query strings containing access tokens.
        for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                     "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
            logging.getLogger(name).disabled = True
        self.http = httpx.AsyncClient(
            transport=transport, verify=ssl.create_default_context(), trust_env=False,
            follow_redirects=False, timeout=settings.upstream_timeout_seconds,
            limits=httpx.Limits(max_connections=settings.max_concurrent_ocr + 1,
                               max_keepalive_connections=settings.max_concurrent_ocr + 1),
        )
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_ocr)
        self.rate = RateLimiter(settings.requests_per_second)
        self.token_lock = asyncio.Lock()
        self._token = ""
        self._refresh_at = 0.0

    async def close(self):
        self._token = ""
        self._refresh_at = 0
        await self.http.aclose()

    async def request(self, url, *, content, params=None):
        try:
            async with asyncio.timeout(self.settings.upstream_timeout_seconds):
                async with self.http.stream(
                    "POST", url, content=content, params=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        return None, True
                    if response.status_code != 200:
                        raise ServiceError("upstream_http_error")
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ServiceError("upstream_invalid_response")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(65536):
                        data.extend(chunk)
                        if len(data) > self.settings.max_upstream_response_bytes:
                            raise ServiceError("upstream_response_too_large")
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise ValueError
                    return result, False
        except ServiceError:
            raise
        except (httpx.TransportError, TimeoutError):
            return None, True
        except (ValueError, TypeError, UnicodeError):
            raise ServiceError("upstream_invalid_response") from None

    async def get_token(self, rejected=None):
        async with self.token_lock:
            now = time.monotonic()
            if self._token and now < self._refresh_at and self._token != rejected:
                return self._token
            self._token = ""
            self._refresh_at = 0
            body = urlencode({
                "grant_type": "client_credentials",
                "client_id": self.settings.api_key,
                "client_secret": self.settings.secret_key,
            }).encode("ascii")
            for attempt in range(2):
                await self.rate.wait()
                result, transient = await self.request(self.settings.auth_url, content=body)
                if not transient:
                    break
                if attempt == 1:
                    raise ServiceError("upstream_auth_unavailable")
            token = result.get("access_token")
            expires = result.get("expires_in")
            if (not isinstance(token, str) or not 0 < len(token) <= 8192
                    or any(ord(c) < 33 or ord(c) > 126 for c in token)
                    or isinstance(expires, bool)):
                raise ServiceError("upstream_auth_failed")
            try:
                expires = float(expires)
                if not 0 < expires <= 365 * 24 * 3600:
                    raise ValueError
            except (ValueError, TypeError):
                raise ServiceError("upstream_auth_failed") from None
            self._token = token
            self._refresh_at = time.monotonic() + expires - min(60, expires * 0.1)
            return token

    async def recognize(self, body: bytes) -> list[str]:
        if not form_fits(body):
            raise ServiceError("encoded_page_too_large", "invalidParseFile")
        token = await self.get_token()
        refreshed = False
        transient_retry = False
        while True:
            async with self.semaphore:
                await self.rate.wait()
                result, transient = await self.request(
                    self.settings.ocr_url, content=body, params={"access_token": token},
                )
            code = result.get("error_code") if result is not None else None
            if code in (110, 111, "110", "111"):
                if refreshed:
                    raise ServiceError("upstream_token_rejected")
                token = await self.get_token(rejected=token)
                refreshed = True
                continue
            if transient or code in (18, 282000, "18", "282000"):
                if transient_retry:
                    raise ServiceError("upstream_unavailable")
                transient_retry = True
                continue
            if code is not None:
                raise ServiceError("upstream_ocr_failed")
            words = result.get("words_result")
            count = result.get("words_result_num")
            if (not isinstance(words, list) or type(count) is not int or count != len(words)
                    or any(not isinstance(item, dict) or not isinstance(item.get("words"), str)
                           for item in words)):
                raise ServiceError("upstream_invalid_response")
            lines = [item["words"] for item in words]
            if sum(map(len, lines)) > self.settings.max_output_chars:
                raise ServiceError("output_limit_exceeded")
            return lines
