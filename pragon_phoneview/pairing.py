"""
pragon_phoneview/pairing.py — QR code generation for phone pairing.

Framework-agnostic: returns PNG bytes / a data URI, so it can be dropped into
a PyQt/Tk/web/CLI front end without depending on any of them.

Install: pip install qrcode[pil]
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Optional


def make_qr_png(url: str, box_size: int = 8, border: int = 2) -> Optional[bytes]:
    """Return PNG image bytes for a QR code encoding `url`, or None if the
    `qrcode` package isn't installed."""
    try:
        import qrcode
        qr = qrcode.QRCode(
            box_size=box_size, border=border,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return None


def make_qr_data_uri(url: str, box_size: int = 8, border: int = 2) -> Optional[str]:
    """Return a `data:image/png;base64,...` URI — handy for embedding
    directly into an <img> tag in a web-based launcher UI."""
    png = make_qr_png(url, box_size=box_size, border=border)
    if png is None:
        return None
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
