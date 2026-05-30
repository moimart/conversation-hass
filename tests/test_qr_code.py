"""Tests for the QR code PNG generator."""

import io
from unittest.mock import patch

import pytest

from server.app.qr_code import generate_qr_png


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestGenerateQrPng:
    def test_returns_valid_png_bytes(self):
        out = generate_qr_png("hello world")
        assert isinstance(out, bytes)
        assert out.startswith(PNG_MAGIC)
        assert len(out) > 100  # any real PNG is bigger than this

    def test_decodes_to_pil_image(self):
        """The returned bytes should be a real PNG decodable by PIL."""
        from PIL import Image
        out = generate_qr_png("ok")
        img = Image.open(io.BytesIO(out))
        img.verify()  # raises on a corrupt PNG
        assert img.format == "PNG"

    def test_empty_string_still_returns_png(self):
        # qrcode handles empty payloads (it just encodes the empty string).
        out = generate_qr_png("")
        assert isinstance(out, bytes)
        assert out.startswith(PNG_MAGIC)

    def test_long_url(self):
        # A long URL should still encode (qrcode picks a larger version).
        url = "https://example.com/very/long/path?" + "x=1&" * 50
        out = generate_qr_png(url)
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_unicode_payload(self):
        out = generate_qr_png("héllo — über")
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_box_size_affects_output_size(self):
        small = generate_qr_png("same", box_size=4)
        big = generate_qr_png("same", box_size=20)
        assert small is not None and big is not None
        # Same content, bigger box_size → bigger image → more PNG bytes.
        assert len(big) > len(small)

    def test_border_param_accepted(self):
        out = generate_qr_png("data", border=4)
        assert out is not None
        assert out.startswith(PNG_MAGIC)

    def test_returns_none_on_exception(self):
        """If qrcode raises (corrupted lib, etc.), the wrapper returns None."""
        # Patch the qrcode.QRCode constructor to blow up — simulates any
        # internal failure path; the function should catch and return None.
        with patch("qrcode.QRCode", side_effect=RuntimeError("boom")):
            assert generate_qr_png("anything") is None
