from pathlib import Path

from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.requests import Request

from .config import Settings
from .errors import ServiceError


class UploadParser:
    def __init__(self, path: Path, settings: Settings):
        self.path = path
        self.settings = settings
        self.handle = None
        self.files = 0
        self.parts = 0
        self.ended = False
        self.headers = {}
        self.header_name = bytearray()
        self.header_value = bytearray()
        self.header_size = 0
        self.part_size = 0
        self.is_file = False

    def invalid(self):
        return ServiceError("invalid_multipart", "invalidParseFile", 400)

    def on_part_begin(self):
        self.parts += 1
        if self.parts > 33:
            raise self.invalid()
        self.headers = {}
        self.header_size = 0
        self.part_size = 0
        self.is_file = False

    def on_header_field(self, data, start, end):
        self.header_size += end - start
        if self.header_size > 8192:
            raise self.invalid()
        self.header_name.extend(data[start:end])

    def on_header_value(self, data, start, end):
        self.header_size += end - start
        if self.header_size > 8192:
            raise self.invalid()
        self.header_value.extend(data[start:end])

    def on_header_end(self):
        name = bytes(self.header_name).lower()
        if name in self.headers:
            raise self.invalid()
        self.headers[name] = bytes(self.header_value)
        self.header_name.clear()
        self.header_value.clear()

    def on_headers_finished(self):
        kind, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        if kind != b"form-data" or not options.get(b"name"):
            raise self.invalid()
        self.is_file = b"filename" in options
        if self.is_file:
            self.files += 1
            if self.files != 1 or options[b"name"] != b"file":
                raise ServiceError("exactly_one_file_required", "invalidParseFile", 400)
            self.handle = self.path.open("xb")

    def on_part_data(self, data, start, end):
        self.part_size += end - start
        if self.is_file:
            if self.part_size > self.settings.max_file_bytes:
                raise ServiceError("file_too_large", "invalidParseFile", 413)
            self.handle.write(data[start:end])
        elif self.part_size > 65536:
            raise self.invalid()

    def on_part_end(self):
        if self.is_file:
            self.handle.close()
            self.handle = None
            if self.part_size == 0:
                raise ServiceError("empty_file", "invalidParseFile", 400)

    def on_end(self):
        self.ended = True

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


async def read_upload(request: Request, directory: Path, settings: Settings) -> Path:
    lengths = request.headers.getlist("content-length")
    if lengths:
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise ServiceError("invalid_content_length", "invalidParseFile", 400)
        if int(lengths[0]) > settings.max_body_bytes:
            raise ServiceError("body_too_large", "invalidParseFile", 413)
    try:
        kind, options = parse_options_header(request.headers.get("content-type", ""))
    except (ValueError, TypeError):
        raise ServiceError("invalid_multipart", "invalidParseFile", 400) from None
    boundary = options.get(b"boundary", b"")
    if (kind != b"multipart/form-data" or not boundary or len(boundary) > 200
            or any(c < 33 or c > 126 for c in boundary)):
        raise ServiceError("invalid_multipart", "invalidParseFile", 400)
    path = directory / "input"
    state = UploadParser(path, settings)
    callbacks = {name: getattr(state, name) for name in (
        "on_part_begin", "on_header_field", "on_header_value", "on_header_end",
        "on_headers_finished", "on_part_data", "on_part_end", "on_end",
    )}
    parser = MultipartParser(boundary, callbacks)
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > settings.max_body_bytes:
                raise ServiceError("body_too_large", "invalidParseFile", 413)
            parser.write(chunk)
        parser.finalize()
        if not state.ended or state.files != 1:
            raise ServiceError("incomplete_or_missing_file", "invalidParseFile", 400)
        if lengths and received != int(lengths[0]):
            raise ServiceError("invalid_content_length", "invalidParseFile", 400)
    except ServiceError:
        raise
    except (ValueError, TypeError):
        raise ServiceError("invalid_multipart", "invalidParseFile", 400) from None
    finally:
        state.close()
        parser.close()
    return path
