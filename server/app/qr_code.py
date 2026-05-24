"""QR code generation utility for rendering URLs on the kiosk orb."""

from __future__ import annotations

import io
import logging

log = logging.getLogger("hal.qr")


def generate_qr_png(
    data: str,
    box_size: int = 10,
    border: int = 2,
) -> bytes:
    """Generate a QR code as PNG bytes (white on black for dark kiosk themes).

    Returns empty bytes on failure so callers can treat it as a no-op.
    """
    if not data:
        return b""
    try:
        import qrcode
        from qrcode.image.pil import PilImage

        qr = qrcode.QRCode(box_size=box_size, border=border)
        qr.add_data(data)
        qr.make(fit=True)
        img: PilImage = qr.make_image(fill_color="white", back_color="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log.error(f"QR generation failed for {data[:80]!r}: {e}")
        return b""
