import asyncio
import json
import os
import sys
from pathlib import Path

from .config import Settings
from .encoding import BAIDU_MAX_FORM_BYTES
from .errors import ServiceError


class Renderer:
    def __init__(self, source: Path, settings: Settings):
        self.source = source
        self.settings = settings
        self.process = None
        self.pages = 0
        self.index = 0

    async def __aenter__(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.render_worker", str(self.source),
            json.dumps(self.settings.render_options()),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4096,
            cwd=Path(__file__).resolve().parents[1],
            env={"PATH": os.defpath, "LANG": "C.UTF-8",
                 "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            result = await self.message()
            count = result.get("pages")
            if type(count) is not int or not 0 < count <= self.settings.max_pages:
                raise ServiceError("render_worker_failed")
            self.pages = count
            return self
        except BaseException:
            await self.close()
            raise

    async def message(self):
        try:
            line = await self.process.stdout.readline()
            result = json.loads(line)
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise ServiceError("render_worker_failed") from None
        if "error" in result:
            raise ServiceError(result["error"], result["statusText"])
        return result

    async def next_page(self) -> bytes:
        self.process.stdin.write(b"next\n")
        try:
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise ServiceError("render_worker_failed") from None
        result = await self.message()
        self.index += 1
        size = result.get("bytes")
        if (result.get("page") != self.index or type(size) is not int
                or not 0 < size < BAIDU_MAX_FORM_BYTES):
            raise ServiceError("render_worker_failed")
        path = self.source.parent / "page.form"
        if path.stat().st_size != size:
            raise ServiceError("render_worker_failed")
        body = await asyncio.to_thread(path.read_bytes)
        path.unlink()
        return body

    async def _stop(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        try:
            async with asyncio.timeout(1):
                await self.process.wait()
        except TimeoutError:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()
        self.process.stdin.close()

    async def close(self):
        task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()
