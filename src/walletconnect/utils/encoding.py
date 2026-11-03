"""
Base encodings used by WalletConnect (unpadded base64url and Bitcoin base58).
"""

from __future__ import annotations

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b64url_encode(data: bytes) -> str:
    """Encode ``data`` as unpadded URL-safe base64."""
    import base64

    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    """Decode unpadded URL-safe base64."""
    import base64

    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def b58encode(data: bytes) -> str:
    """Encode ``data`` with the Bitcoin base58 alphabet (no check)."""
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n:
        n, rem = divmod(n, 58)
        out.append(_B58_ALPHABET[rem])
    pad = 0
    for byte in data:
        if byte == 0:
            pad += 1
        else:
            break
    return (_B58_ALPHABET[0:1] * pad + out[::-1]).decode("ascii")


def b58decode(data: str) -> bytes:
    """Decode Bitcoin base58 (no check)."""
    n = 0
    for char in data:
        try:
            value = _B58_ALPHABET.index(char.encode("ascii"))
        except ValueError as exc:
            raise ValueError(f"Invalid base58 character {char!r}") from exc
        n = n * 58 + value
    full = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = 0
    for char in data:
        if char == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + full
