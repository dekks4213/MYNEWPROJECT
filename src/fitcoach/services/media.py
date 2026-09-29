"""Validation and minimization of user media before it leaves the server.

Images are decoded, bounded, converted to RGB and re-encoded as JPEG: this drops EXIF
(including GPS) and any trailing payload. Audio is only size/format checked.
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from fitcoach.services.errors import ServiceError

MAX_PIXELS = 40_000_000
MAX_SIDE = 1280
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")


def _is_webp(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def prepare_image(data: bytes, max_bytes: int) -> tuple[bytes, str]:
    if len(data) > max_bytes:
        raise ServiceError("media_too_large")
    if not (data.startswith(_IMAGE_MAGIC) or _is_webp(data)):
        raise ServiceError("bad_media")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise ServiceError("media_too_large")
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            rgb = img.convert("RGB")
    except ServiceError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise ServiceError("bad_media") from exc
    rgb.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=85, optimize=True)  # no exif passed -> stripped
    return out.getvalue(), "image/jpeg"


def check_voice(data: bytes, *, duration_s: int | None, max_bytes: int, max_seconds: int) -> str:
    if len(data) > max_bytes or (duration_s is not None and duration_s > max_seconds):
        raise ServiceError("media_too_large")
    if not data.startswith(b"OggS"):
        raise ServiceError("bad_media")
    return "audio/ogg"
