from __future__ import annotations

import base64
import io

import segno

QR_ERROR_LEVEL = "m"
QR_BORDER = 2
PNG_SCALE = 4
SVG_SCALE = 6


def png_b64(text: str) -> str:
    buf = io.BytesIO()
    segno.make(text, error=QR_ERROR_LEVEL).save(buf, kind="png", scale=PNG_SCALE, border=QR_BORDER)
    return base64.b64encode(buf.getvalue()).decode()


def svg_data_uri(text: str, error: str = QR_ERROR_LEVEL) -> str:
    return segno.make(text, error=error).svg_data_uri(scale=SVG_SCALE, border=QR_BORDER)
