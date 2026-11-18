import json
import math
import os
import sys
import warnings
from pathlib import Path

from .errors import ServiceError


def emit(message: dict):
    print(json.dumps(message, separators=(",", ":")), flush=True)


def resource_limits(options):
    if sys.platform == "linux":
        import resource

        memory = options["worker_memory_mb"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        cpu = max(1, math.ceil(options["total_timeout_seconds"]))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def rgb_image(source, Image):
    if source.mode == "RGBA" or "transparency" in source.info:
        rgba = source.convert("RGBA")
        try:
            result = Image.new("RGB", source.size, "white")
            alpha = rgba.getchannel("A")
            try:
                result.paste(rgba, mask=alpha)
            finally:
                alpha.close()
        finally:
            rgba.close()
        return result
    return source.convert("RGB")


def image_form(path, expected, options):
    from PIL import Image, ImageOps
    from .encoding import checked_dimensions, encode_image

    Image.MAX_IMAGE_PIXELS = options["max_source_pixels"]
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(path) as image:
        if image.format != expected or getattr(image, "n_frames", 1) != 1:
            raise ServiceError("unsupported_image_content", "unsupportedParseFileType")
        checked_dimensions(*image.size, options["max_source_pixels"])
        image.verify()
    with Image.open(path) as image:
        checked_dimensions(*image.size, options["max_source_pixels"])
        image.load()
        oriented = ImageOps.exif_transpose(image)
        try:
            rgb = rgb_image(oriented, Image)
            try:
                # Resize before encoding, but never allocate an unbounded source raster.
                factor = min(1, math.sqrt(options["max_render_pixels"] / (rgb.width * rgb.height)))
                if factor < 1:
                    smaller = rgb.resize((max(1, int(rgb.width * factor)), max(1, int(rgb.height * factor))))
                    rgb.close()
                    rgb = smaller
                return encode_image(rgb, options["max_render_pixels"])
            finally:
                rgb.close()
        finally:
            oriented.close()


def pdf_form(doc, index, options):
    from PIL import Image
    from .encoding import checked_dimensions, encode_image

    page = doc[index]
    try:
        width, height = page.get_size()
        nominal = options["render_dpi"] / 72
        checked_dimensions(width * nominal, height * nominal, options["max_source_pixels"])
        scale = min(
            nominal, 8192 / max(width, height),
            math.sqrt(options["max_render_pixels"] / (width * height)),
        )
        # PDFium rounds dimensions up; reserve one pixel on each edge.
        while (math.ceil(width * scale) * math.ceil(height * scale) > options["max_render_pixels"]
               or max(math.ceil(width * scale), math.ceil(height * scale)) > 8192):
            scale *= 0.999
        checked_dimensions(math.ceil(width * scale), math.ceil(height * scale), options["max_render_pixels"])
        bitmap = page.render(scale=scale)
        try:
            pil = bitmap.to_pil()
            try:
                rgb = rgb_image(pil, Image)
                try:
                    return encode_image(rgb, options["max_render_pixels"])
                finally:
                    rgb.close()
            finally:
                pil.close()
        finally:
            bitmap.close()
    finally:
        page.close()


def run(path: Path, options):
    import pypdfium2 as pdfium

    with path.open("rb") as source:
        magic = source.read(8)
    doc = None
    is_pdf = magic.startswith(b"%PDF-")
    try:
        if is_pdf:
            try:
                doc = pdfium.PdfDocument(path)
            except pdfium.PdfiumError as exc:
                encrypted = exc.err_code == pdfium.raw.FPDF_ERR_PASSWORD
                raise ServiceError(
                    "pdf_encrypted" if encrypted else "pdf_corrupted",
                    "pdfEncrypted" if encrypted else "pdfCorrupted",
                ) from None
            if pdfium.raw.FPDF_GetSecurityHandlerRevision(doc.raw) >= 0:
                raise ServiceError("pdf_encrypted", "pdfEncrypted")
            count = len(doc)
            if count <= 0:
                raise ServiceError("pdf_corrupted", "pdfCorrupted")
            if count > options["max_pages"]:
                raise ServiceError("pdf_page_limit_exceeded", "invalidParseFile")
            expected = None
        elif magic.startswith(b"\xff\xd8\xff"):
            count, expected = 1, "JPEG"
        elif magic == b"\x89PNG\r\n\x1a\n":
            count, expected = 1, "PNG"
        else:
            raise ServiceError("unsupported_file_type", "unsupportedParseFileType")
        emit({"pages": count})
        for index in range(count):
            if sys.stdin.buffer.readline(16) != b"next\n":
                return
            body = pdf_form(doc, index, options) if is_pdf else image_form(path, expected, options)
            target = path.parent / "page.form"
            with target.open("xb") as output:
                output.write(body)
            emit({"page": index + 1, "bytes": len(body)})
            del body
    except ServiceError:
        raise
    except Exception:
        raise ServiceError(
            "pdf_corrupted" if is_pdf else "image_corrupted",
            "pdfCorrupted" if is_pdf else "invalidParseFile",
        ) from None
    finally:
        if doc is not None:
            doc.close()


if __name__ == "__main__":
    os.umask(0o077)
    try:
        settings = json.loads(sys.argv[2])
        resource_limits(settings)
        run(Path(sys.argv[1]), settings)
    except ServiceError as error:
        emit({"error": error.code, "statusText": error.status_text})
    except Exception:
        emit({"error": "render_worker_failed", "statusText": "pdfParseFailed"})
