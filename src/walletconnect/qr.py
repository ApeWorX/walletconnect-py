"""
Render pairing URIs as QR codes in a terminal.

Requires the optional ``qr`` extra::

    pip install "walletconnect[qr]"

Two styles are available:

- ``compact=True`` (default): Unicode half blocks (``▀▄█``), two QR rows per text line. Small enough
  for an 80-column terminal and scans well from a screen.
- ``compact=False``: plain 7-bit ASCII (``##`` / two spaces per module). Safe for logs, CI output,
  and chat/bot messages that mangle Unicode (use a monospace font/code block).

By default the output is drawn for a *dark* terminal background (``invert=True``): the "ink"
characters paint the light modules and quiet zone, so the code appears dark-on-light. Pass
``invert=False`` for light backgrounds, or when printing on paper.
"""

import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from .pairing.uri import PairingURI

DEFAULT_BORDER = 2
"""Quiet-zone width in modules. The spec suggests 4; 2 is plenty on screens and saves space."""

ASCII_INK = "##"
ASCII_BLANK = "  "

_HALF_BLOCKS = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}


def _require_segno():  # pragma: no cover - exercised only without the extra installed
    try:
        import segno
    except ImportError as err:
        raise ImportError(
            "QR code rendering requires the 'qr' extra: pip install \"walletconnect[qr]\""
        ) from err

    return segno


def qr_matrix(data: "str | PairingURI", border: int = DEFAULT_BORDER) -> list[list[bool]]:
    """
    Encode ``data`` as a (regular, never Micro) QR code.

    Returns:
        Rows of modules, ``True`` meaning a dark module, including the quiet zone.
    """
    if border < 0:
        raise ValueError("border must be non-negative")

    segno = _require_segno()
    # NOTE: `make_qr` never produces Micro QR codes, which most phone scanners can't read.
    qr = segno.make_qr(str(data), error="l", boost_error=True)
    return [[bool(m) for m in row] for row in qr.matrix_iter(scale=1, border=border)]


def render_matrix(
    matrix: Iterable[Iterable[bool]], compact: bool = True, invert: bool = True
) -> str:
    """Render a module matrix (``True`` = dark) as terminal text."""
    # "ink" = a module drawn with a printed character.
    ink = [[dark != invert for dark in row] for row in matrix]
    if not ink:
        return ""

    if not compact:
        return "\n".join("".join(ASCII_INK if i else ASCII_BLANK for i in row) for row in ink)

    if len(ink) % 2:
        # Pad with a light row (part of the quiet zone).
        ink.append([invert] * len(ink[0]))

    lines = []
    for top, bottom in zip(ink[::2], ink[1::2], strict=True):
        lines.append("".join(_HALF_BLOCKS[pair] for pair in zip(top, bottom, strict=True)))

    return "\n".join(lines)


def terminal_qr(
    data: "str | PairingURI",
    compact: bool = True,
    invert: bool = True,
    border: int = DEFAULT_BORDER,
) -> str:
    """
    Return a printable QR code for ``data`` (e.g. a ``wc:`` pairing URI).

    Args:
        data: The URI (or any string) to encode.
        compact: Use Unicode half blocks (default) instead of pure ASCII.
        invert: Draw for a dark terminal background (default).
        border: Quiet-zone width in modules.
    """
    return render_matrix(qr_matrix(data, border=border), compact=compact, invert=invert)


def print_qr(
    data: "str | PairingURI",
    compact: bool = True,
    invert: bool = True,
    border: int = DEFAULT_BORDER,
    file: TextIO | None = None,
) -> None:
    """Print :func:`terminal_qr` output (defaults to ``sys.stdout``)."""
    out = file or sys.stdout
    out.write(terminal_qr(data, compact=compact, invert=invert, border=border) + "\n")
    out.flush()
