from dataclasses import replace
from io import BytesIO
from urllib.parse import parse_qs

import pytest
from PIL import Image

from app.config import Settings
from app.encoding import (BAIDU_MAX_FORM_BYTES, checked_dimensions, encode_form,
                          encode_image, form_fits)
from app.errors import ServiceError
from app.main import plain_markdown


def test_limits_and_repr(settings):
    assert settings.max_file_bytes == 20 * 1024 * 1024
    assert settings.max_body_bytes == settings.max_file_bytes + 1048576
    assert BAIDU_MAX_FORM_BYTES == 10_000_000
    assert settings.api_key not in repr(settings)
    assert settings.secret_key not in repr(settings)
    assert settings.inbound_token not in repr(settings)


@pytest.mark.parametrize("field,value", [
    ("inbound_token", "short"), ("api_key", ""), ("secret_key", "secret\n"),
    ("max_pages", 0), ("requests_per_second", float("nan")),
    ("max_concurrent_files", 100), ("max_source_pixels", -1),
    ("ocr_url", "http://example.com/ocr"), ("auth_url", "https://user:pass@host/token"),
    ("ocr_url", "https://aip.baidubce.com/ocr?access_token=not-allowed"),
])
def test_bad_configuration(settings, field, value):
    with pytest.raises(ValueError, match="invalid configuration"):
        replace(settings, **{field: value})


def test_http_mock_is_loopback_and_opt_in(settings):
    with pytest.raises(ValueError):
        replace(settings, ocr_url="http://127.0.0.1:8080/ocr")
    assert replace(settings, allow_insecure_http=True,
                   ocr_url="http://127.0.0.1:8080/ocr").allow_insecure_http
    with pytest.raises(ValueError):
        replace(settings, allow_insecure_http=True, ocr_url="http://external.example/ocr")


@pytest.mark.parametrize("placeholder", [
    "replace-with-api-key", "validation-placeholder", "dummy-api-key", "changeme",
])
def test_env_refuses_placeholders(monkeypatch, placeholder):
    monkeypatch.setenv("BAIDU_API_KEY", placeholder)
    monkeypatch.setenv("BAIDU_SECRET_KEY", "unit-test-secret")
    monkeypatch.setenv("FASTGPT_OCR_TOKEN", "unit-test-long-token-12345678901234567890")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_env_missing_and_valid(monkeypatch):
    for name in ("BAIDU_API_KEY", "BAIDU_SECRET_KEY", "FASTGPT_OCR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("BAIDU_API_KEY", "unit-test-key-not-real")
    monkeypatch.setenv("BAIDU_SECRET_KEY", "unit-test-secret-not-real")
    monkeypatch.setenv("FASTGPT_OCR_TOKEN", "unit-test-long-token-12345678901234567890")
    monkeypatch.setenv("OCR_TOTAL_TIMEOUT_SECONDS", "0.25")
    assert Settings.from_env().total_timeout_seconds == 0.25
    monkeypatch.setenv("OCR_ALLOW_INSECURE_HTTP", "yes")
    with pytest.raises(ValueError, match="OCR_ALLOW_INSECURE_HTTP"):
        Settings.from_env()


def test_real_urlencoded_form_and_exact_limit():
    form = encode_form(b"\xff" * 100)
    assert len(form) > 3 * 100
    fields = parse_qs(form.decode())
    assert fields["image"][0].startswith("////")
    assert fields["detect_direction"] == ["true"]
    assert fields["multidirectional_recognize"] == ["true"]
    assert fields["language_type"] == ["CHN_ENG"]
    assert form_fits(b"x" * (BAIDU_MAX_FORM_BYTES - 1))
    assert not form_fits(b"x" * BAIDU_MAX_FORM_BYTES)


@pytest.mark.parametrize("width,height", [(14, 50), (50, 14), (float("nan"), 30),
                                             (10001, 10001)])
def test_pixel_limit(width, height):
    with pytest.raises(ServiceError):
        checked_dimensions(width, height, 40_000_000)


def test_jpeg_rgb_metadata_and_max_edge():
    with Image.new("RGB", (9000, 20), "white") as image:
        image.info["exif"] = b"sensitive-metadata"
        form = encode_image(image, 16_000_000)
    import base64
    with Image.open(BytesIO(base64.b64decode(parse_qs(form.decode())["image"][0]))) as result:
        assert result.format == "JPEG"
        assert result.mode == "RGB"
        assert result.width == 8192
        assert min(result.size) >= 15
        assert not result.getexif()


def test_encoding_reduction_is_bounded(monkeypatch):
    import app.encoding as encoding
    calls = []
    def never_fits(body):
        calls.append(len(body))
        return False
    monkeypatch.setattr(encoding, "form_fits", never_fits)
    with Image.new("RGB", (200, 200), "white") as image:
        with pytest.raises(ServiceError, match="encoded_page_too_large"):
            encode_image(image, 16_000_000)
    assert len(calls) == 10


def test_markdown_is_inert():
    value = plain_markdown([
        "![secret](https://private.example/file) <img src='https://host/x'>",
        "[ref]: https://host/file", "<https://host/file> &lt;img&gt;",
    ])
    assert "https://" not in value
    assert "<img" not in value
    assert "\\!\\[secret\\]\\(" in value
    assert "&lt;img" in value
    assert "&amp;lt;" in value
