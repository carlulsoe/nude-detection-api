import warnings
from io import BytesIO

import numpy as np
from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

EXPLICIT_LABELS = frozenset(
    {
        "FEMALE_BREAST_EXPOSED",
        "FEMALE_GENITALIA_EXPOSED",
        "MALE_GENITALIA_EXPOSED",
        "BUTTOCKS_EXPOSED",
        "ANUS_EXPOSED",
    }
)


def decode_image(body: bytes, max_pixels: int) -> np.ndarray:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise HTTPException(415, "Use JPEG, PNG, or WebP")
                if getattr(image, "n_frames", 1) != 1:
                    raise HTTPException(422, "Animated images are not supported")
                if image.width * image.height > max_pixels:
                    raise HTTPException(413, "Image exceeds the pixel limit")
                image.load()
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((1280, 1280))
                # NudeNET accepts OpenCV BGR arrays. No temporary image files.
                return np.asarray(image)[:, :, ::-1].copy()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise HTTPException(422, "Invalid or oversized image") from exc


def detect(detector, pixels: np.ndarray, threshold: float) -> dict:
    detections = [
        {
            "label": item["class"],
            "confidence": float(item["score"]),
            "box": [int(value) for value in item["box"]],
        }
        for item in detector.detect(pixels)
    ]
    score = max(
        (d["confidence"] for d in detections if d["label"] in EXPLICIT_LABELS),
        default=0.0,
    )
    return {
        "nsfw": score >= threshold,
        "score": score,
        "threshold": threshold,
        "model": "nudenet-320n",
        "policy": "exposed-parts-v1",
        "image": {"width": pixels.shape[1], "height": pixels.shape[0]},
        "detections": detections,
    }
