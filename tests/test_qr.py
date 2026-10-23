import io

import pytest
import segno

from walletconnect.pairing.uri import PairingURI
from walletconnect.qr import (
    ASCII_BLANK,
    ASCII_INK,
    print_qr,
    qr_matrix,
    render_matrix,
    terminal_qr,
)

URI = PairingURI.generate()


def _decode_ascii(text: str) -> list[list[bool]]:
    rows = []
    for line in text.splitlines():
        cells = [line[i : i + 2] for i in range(0, len(line), 2)]
        assert set(cells) <= {ASCII_INK, ASCII_BLANK}
        rows.append([c == ASCII_INK for c in cells])

    return rows


def _decode_compact(text: str) -> list[list[bool]]:
    top_bottom = {"█": (True, True), "▀": (True, False), "▄": (False, True), " ": (False, False)}
    rows = []
    for line in text.splitlines():
        pairs = [top_bottom[ch] for ch in line]
        rows.append([p[0] for p in pairs])
        rows.append([p[1] for p in pairs])

    return rows


def test_matrix_is_regular_qr_with_quiet_zone():
    matrix = qr_matrix(URI, border=2)
    size = len(matrix)
    assert all(len(row) == size for row in matrix)
    # Version 1 QR is 21 modules; Micro QR would be smaller. Plus 2 * border.
    assert size >= 21 + 4
    # Quiet zone is light.
    assert not any(matrix[0])
    assert not any(row[0] for row in matrix)
    # Finder pattern corner (dark) right after the quiet zone.
    assert matrix[2][2]


def test_matrix_matches_segno():
    expected = segno.make_qr(str(URI), error="l").matrix_iter(scale=1, border=1)
    assert qr_matrix(str(URI), border=1) == [[bool(m) for m in row] for row in expected]


@pytest.mark.parametrize("invert", [True, False])
def test_ascii_round_trip(invert):
    matrix = qr_matrix(URI)
    text = terminal_qr(URI, compact=False, invert=invert)
    assert text.isascii()
    ink = _decode_ascii(text)
    assert ink == [[dark != invert for dark in row] for row in matrix]


@pytest.mark.parametrize("invert", [True, False])
def test_compact_round_trip(invert):
    matrix = qr_matrix(URI)
    text = terminal_qr(URI, invert=invert)
    lines = text.splitlines()
    assert len(lines) == (len(matrix) + 1) // 2
    ink = _decode_compact(text)[: len(matrix)]
    assert ink == [[dark != invert for dark in row] for row in matrix]


def test_compact_odd_padding_is_light():
    # A 1x1 dark module: inverted padding row must be "ink" (light).
    assert render_matrix([[True]], compact=True, invert=True) == "▄"
    assert render_matrix([[True]], compact=True, invert=False) == "▀"
    assert render_matrix([], compact=True) == ""


def test_compact_fits_80_columns():
    width = max(len(line) for line in terminal_qr(URI).splitlines())
    assert width <= 80


def test_print_qr():
    out = io.StringIO()
    print_qr(URI, compact=False, file=out)
    assert out.getvalue() == terminal_qr(URI, compact=False) + "\n"


def test_negative_border():
    with pytest.raises(ValueError, match="non-negative"):
        qr_matrix(URI, border=-1)
