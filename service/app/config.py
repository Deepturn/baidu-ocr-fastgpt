import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    inbound_token: str = field(repr=False)
    ocr_url: str = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
    auth_url: str = "https://aip.baidubce.com/oauth/2.0/token"
    max_file_mb: int = 20
    max_pages: int = 100
    max_concurrent_files: int = 2
    max_concurrent_ocr: int = 1
    requests_per_second: float = 1
    total_timeout_seconds: float = 540
    upstream_timeout_seconds: float = 60
    max_source_pixels: int = 40_000_000
    max_render_pixels: int = 16_000_000
    render_dpi: int = 200
    multipart_overhead_bytes: int = 1_048_576
    max_output_chars: int = 2_000_000
    max_upstream_response_bytes: int = 8_388_608
    worker_memory_mb: int = 512
    allow_insecure_http: bool = False

    def __post_init__(self):
        for name in ("api_key", "secret_key", "inbound_token"):
            value = getattr(self, name)
            if not value or value != value.strip() or len(value) > 4096:
                raise ValueError(f"invalid configuration: {name}")
            if any(ord(char) < 32 or ord(char) > 126 for char in value):
                raise ValueError(f"invalid configuration: {name}")
        if len(self.inbound_token) < 32:
            raise ValueError("invalid configuration: inbound_token")
        limits = {
            "max_file_mb": 100, "max_pages": 1000,
            "max_concurrent_files": 8, "max_concurrent_ocr": 8,
            "requests_per_second": 100, "total_timeout_seconds": 3600,
            "upstream_timeout_seconds": 300, "max_source_pixels": 100_000_000,
            "max_render_pixels": 40_000_000, "render_dpi": 600,
            "multipart_overhead_bytes": 4_194_304, "max_output_chars": 10_000_000,
            "max_upstream_response_bytes": 16_777_216, "worker_memory_mb": 2048,
        }
        for name, maximum in limits.items():
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f"invalid configuration: {name}")
        for name in ("ocr_url", "auth_url"):
            url = urlsplit(getattr(self, name))
            local_http = (
                self.allow_insecure_http and url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            if (not url.hostname or (url.scheme != "https" and not local_http)
                    or url.username or url.password or url.query or url.fragment):
                raise ValueError(f"invalid configuration: {name}")
            try:
                url.port
            except ValueError:
                raise ValueError(f"invalid configuration: {name}") from None

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def max_body_bytes(self) -> int:
        return self.max_file_bytes + self.multipart_overhead_bytes

    def render_options(self) -> dict:
        return {key: getattr(self, key) for key in (
            "max_pages", "max_source_pixels", "max_render_pixels",
            "render_dpi", "worker_memory_mb", "total_timeout_seconds",
        )}

    @classmethod
    def from_env(cls):
        required = {}
        for name, env in (("api_key", "BAIDU_API_KEY"),
                          ("secret_key", "BAIDU_SECRET_KEY"),
                          ("inbound_token", "FASTGPT_OCR_TOKEN")):
            required[name] = os.environ.get(env, "")
        kwargs = dict(required)
        for name, value in required.items():
            normalized = value.lower()
            if (normalized.startswith(("replace-with-", "changeme", "dummy", "example"))
                    or "placeholder" in normalized):
                raise ValueError(f"invalid configuration: {name}")
        defaults = cls(**{name: "configuration-default-validation-12345" for name in required})
        for name in cls.__dataclass_fields__:
            if name in required:
                continue
            env = {"ocr_url": "BAIDU_OCR_URL", "auth_url": "BAIDU_AUTH_URL"}.get(
                name, "OCR_" + name.upper()
            )
            raw = os.environ.get(env)
            if raw is None:
                continue
            default = getattr(defaults, name)
            try:
                if isinstance(default, bool):
                    if raw.lower() not in {"true", "false"}:
                        raise ValueError
                    kwargs[name] = raw.lower() == "true"
                elif isinstance(default, (int, float)):
                    kwargs[name] = float(raw) if name in {
                        "requests_per_second", "total_timeout_seconds",
                        "upstream_timeout_seconds",
                    } else int(raw)
                else:
                    kwargs[name] = raw
            except ValueError:
                raise ValueError(f"invalid configuration: {env}") from None
        return cls(**kwargs)
