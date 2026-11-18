import asyncio
import hmac
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from .baidu import BaiduClient
from .config import Settings
from .errors import ServiceError
from .render import Renderer
from .upload import read_upload


logger = logging.getLogger("ocr_adapter")


def plain_markdown(lines: list[str]) -> str:
    escaped = []
    for line in lines:
        clean = "".join(c for c in line if c in "\n\t" or ord(c) >= 32)
        clean = clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for char in "\\`*_{}[]()!#+-.|":
            clean = clean.replace(char, "\\" + char)
        # Also neutralize autolinks and raw-URL extractors, not only Markdown links.
        clean = clean.replace(":", "&#58;")
        escaped.append(clean)
    return "\n".join(escaped)


async def disconnected(request: Request):
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def process_document(path: Path, settings: Settings, baidu: BaiduClient) -> dict:
    contents = []
    total_chars = 0
    has_text = False
    async with Renderer(path, settings) as renderer:
        for index in range(renderer.pages):
            try:
                body = await renderer.next_page()
                lines = await baidu.recognize(body)
                del body
            except ServiceError as error:
                raise ServiceError(f"page_{index + 1}:{error.code}", error.status_text) from None
            has_text = has_text or any(line.strip() for line in lines)
            text = plain_markdown(lines)
            total_chars += len(text)
            if total_chars > settings.max_output_chars:
                raise ServiceError("output_limit_exceeded")
            contents.append(text)
    if not has_text:
        raise ServiceError("no_text_recognized")
    return {"success": True, "markdown": "\n\n".join(contents),
            "pages": renderer.pages, "error": ""}


async def with_disconnect(request: Request, work):
    task = asyncio.create_task(work)
    watch = asyncio.create_task(disconnected(request))
    try:
        done, _ = await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
        if watch in done:
            raise ServiceError("client_disconnected", "invalidParseFile", 400)
        return await task
    finally:
        task.cancel()
        watch.cancel()
        await asyncio.gather(task, watch, return_exceptions=True)


def create_app(settings: Settings | None = None, *, transport=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        config = settings if settings is not None else Settings.from_env()
        application.state.settings = config
        application.state.baidu = BaiduClient(config, transport=transport)
        application.state.active_files = 0
        # Multipart diagnostics may contain client-controlled header bytes.
        logging.getLogger("python_multipart.multipart").disabled = True
        try:
            yield
        finally:
            await application.state.baidu.close()

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @application.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse(ServiceError("invalid_request", "invalidParseFile", error.status_code).payload(),
                            status_code=error.status_code)

    @application.get("/healthz")
    async def health():
        return {"status": "ok"}

    @application.post("/v2/parse/file")
    async def parse_file(request: Request):
        config = application.state.settings
        auth_headers = request.headers.getlist("authorization")
        expected = ("Bearer " + config.inbound_token).encode("ascii")
        provided = auth_headers[0].encode("latin-1") if len(auth_headers) == 1 else b""
        if not hmac.compare_digest(provided, expected):
            return JSONResponse(
                ServiceError("unauthorized", "invalidParseFile", 401).payload(), status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if application.state.active_files >= config.max_concurrent_files:
            return JSONResponse(ServiceError("service_busy", "invalidParseFile", 429).payload(),
                                status_code=429, headers={"Retry-After": "1"})
        # No await between the check and increment: admission is atomic on this event loop.
        application.state.active_files += 1
        try:
            async with asyncio.timeout(config.total_timeout_seconds):
                with tempfile.TemporaryDirectory(prefix="ocr-adapter-", dir="/tmp") as temporary:
                    path = await read_upload(request, Path(temporary), config)
                    result = await with_disconnect(
                        request, process_document(path, config, application.state.baidu),
                    )
                    return JSONResponse(result)
        except ServiceError as error:
            logger.info("parse_failed code=%s status=%s", error.code, error.http_status)
            return JSONResponse(error.payload(), status_code=error.http_status)
        except TimeoutError:
            logger.info("parse_failed code=total_timeout")
            return JSONResponse(ServiceError("total_timeout").payload())
        except ClientDisconnect:
            return JSONResponse(ServiceError("client_disconnected", "invalidParseFile", 400).payload(),
                                status_code=400)
        except Exception:
            # Exception messages/tracebacks may contain URLs, credentials or file content.
            logger.error("parse_failed code=internal_error")
            return JSONResponse(ServiceError("internal_error").payload())
        finally:
            application.state.active_files -= 1

    return application


app = create_app()
