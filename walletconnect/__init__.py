"""
WalletConnect v2 (Reown) client implementation in Python.

NOTE: The protocol implementation has not landed yet. See the README for project status.
"""

from importlib.metadata import (
    PackageNotFoundError as _PackageNotFoundError,
    version as _version,
)

try:
    __version__ = _version(__name__)
except _PackageNotFoundError:
    # package is not installed
    __version__ = "<unknown>"

__all__ = ["__version__"]
