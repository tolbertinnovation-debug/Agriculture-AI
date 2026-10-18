"""Cheap on-device-style checks that run before spending money on a model call.

A blurry, dark or tiny photo produces a confident-sounding wrong diagnosis; asking
the farmer to retake it is better for them and cheaper for the partner.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

MIN_SIDE_PX = 224
MAX_SIDE_PX = 1280  # plenty for leaf symptoms; keeps storage and model tokens down
DARK_MEAN = 35
BRIGHT_MEAN = 235
BLUR_EDGE_STDDEV = 4.0  # lenient: a needless retake request costs the farmer airtime


@dataclass
class ImageCheck:
    ok: bool
    reason: str | None
    jpeg: bytes | None  # normalised, resized JPEG ready for storage and the model
    width: int = 0
    height: int = 0


def check_and_normalize(data: bytes) -> ImageCheck:
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # phones store rotation in EXIF
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return ImageCheck(False, "not an image", None)

    w, h = img.size
    if min(w, h) < MIN_SIDE_PX:
        return ImageCheck(False, "too small", None, w, h)

    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    gray = img.convert("L")
    mean = ImageStat.Stat(gray).mean[0]
    if mean < DARK_MEAN:
        return ImageCheck(False, "too dark", None, *img.size)
    if mean > BRIGHT_MEAN:
        return ImageCheck(False, "overexposed", None, *img.size)

    probe = gray.copy()
    probe.thumbnail((512, 512))
    edges = probe.filter(ImageFilter.FIND_EDGES)
    pw, ph = edges.size
    edges = edges.crop((2, 2, pw - 2, ph - 2))  # the filter sees the frame itself as an edge
    if ImageStat.Stat(edges).stddev[0] < BLUR_EDGE_STDDEV:
        return ImageCheck(False, "blurry", None, *img.size)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)  # also strips EXIF incl. GPS
    return ImageCheck(True, None, out.getvalue(), *img.size)
