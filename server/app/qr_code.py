"""Generate QR code PNGs for displaying on the kiosk orb."""

from __future__ import annotations

import io
import logging

log = logging.getLogger("hal.qr_code")


def generate_qr_png(
    data: str, box_size: int = 10, border: int = 2
) -> bytes | None:
    try:
        import qrcode
        from qrcode.image.pil import PilImage

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=box_size,
            border=border,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img: PilImage = qr.make_image(fill_color="white", back_color="black")  # type: ignore[assignment]
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log.error(f"QR generation failed: {e}")
        return None
