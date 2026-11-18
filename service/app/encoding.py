import base64
import math
from io import BytesIO
from urllib.parse import urlencode

from PIL import Image

from .errors import ServiceError


BAIDU_MAX_FORM_BYTES = 10_000_000


def encode_form(jpeg: bytes) -> bytes:
    return urlencode({
        "image": base64.b64encode(jpeg).decode("ascii"),
        "detect_direction": "true",
        "language_type": "CHN_ENG",
        "multidirectional_recognize": "true",
    }).encode("ascii")


def form_fits(body: bytes) -> bool:
    return len(body) < BAIDU_MAX_FORM_BYTES


def checked_dimensions(width: float, height: float, maximum: int):
    if (not math.isfinite(width) or not math.isfinite(height)
            or width < 15 or height < 15 or width * height > maximum):
        raise ServiceError("image_dimensions_exceeded", "invalidParseFile")


def encode_image(image: Image.Image, max_pixels: int) -> bytes:
    checked_dimensions(*image.size, max_pixels)
    width, height = image.size
    factor = min(1, 8192 / max(width, height), math.sqrt(max_pixels / (width * height)))
    if factor < 1:
        resized = image.resize((max(1, int(width * factor)), max(1, int(height * factor))))
    else:
        resized = image.copy()
    try:
        for attempt in range(10):
            checked_dimensions(*resized.size, max_pixels)
            with BytesIO() as output:
                resized.save(output, format="JPEG", quality=(88, 75, 60)[attempt % 3])
                body = encode_form(output.getvalue())
            if form_fits(body):
                return body
            if attempt % 3 == 2:
                smaller = resized.resize((int(resized.width * 0.75), int(resized.height * 0.75)))
                resized.close()
                resized = smaller
        raise ServiceError("encoded_page_too_large", "invalidParseFile")
    finally:
        resized.close()
