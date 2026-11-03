"""
Serializable client state: the relay identity key and settled sessions.

The library never writes state anywhere on its own. Take a snapshot with
:meth:`SignClient.export_state() <walletconnect.sign.SignClient.export_state>`, store it however
you like (file, database, S3, a secrets manager…), and pass it back with
``SignClient(state=...)`` to resume the same sessions later.

.. warning::

    A :class:`ClientState` contains **secrets**: the relay identity key and each session's
    symmetric encryption key. Anyone holding it can read and send messages on those sessions
    (e.g. ask the wallet to sign). Store it like a password: encrypted at rest, with tight
    access permissions, and never in logs or source control.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from walletconnect.constants import EIP155_NAMESPACE, RELAY_PROTOCOL
from walletconnect.crypto.jwt import AuthKeyPair, generate_auth_key_pair

STATE_VERSION: Literal[1] = 1

_HEX = frozenset("0123456789abcdef")


def _check_hex32(value: str, name: str) -> str:
    value = value.lower().removeprefix("0x")
    if len(value) != 64 or any(c not in _HEX for c in value):
        raise ValueError(f"{name} must be 32 bytes of hex (64 characters)")
    return value


def _new_relay_key() -> str:
    return generate_auth_key_pair().private_key


class Session(BaseModel):
    """A settled WalletConnect session (as seen by the dApp)."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    topic: str
    """Session topic, ``sha256(sym_key)``."""

    sym_key: str = Field(repr=False)
    """Session symmetric key (hex). **Secret.**"""

    expiry: int
    """Unix timestamp (seconds) after which the session is no longer valid."""

    acknowledged: bool = True
    controller: dict[str, Any] = Field(default_factory=dict)
    self_public_key: str = ""
    peer_public_key: str = ""
    peer_metadata: dict[str, Any] = Field(default_factory=dict)
    namespaces: dict[str, Any] = Field(default_factory=dict)
    relay_protocol: str = RELAY_PROTOCOL
    pairing_topic: str | None = None

    @field_validator("sym_key")
    @classmethod
    def _validate_sym_key(cls, value: str) -> str:
        return _check_hex32(value, "sym_key")

    @property
    def accounts(self) -> list[str]:
        """All CAIP-10 accounts approved in the session, across namespaces."""
        accounts: list[str] = []
        for ns in self.namespaces.values():
            accounts.extend(ns.get("accounts") or [])
        return accounts

    @property
    def eip155_accounts(self) -> list[str]:
        """CAIP-10 accounts in the ``eip155`` namespace (e.g. ``eip155:1:0xabc…``)."""
        ns = self.namespaces.get(EIP155_NAMESPACE) or {}
        return list(ns.get("accounts") or [])

    @property
    def peer_name(self) -> str:
        """The wallet's display name, if it sent one."""
        return str(self.peer_metadata.get("name") or "")

    def is_expired(self, now: float | None = None) -> bool:
        return self.expiry <= (time.time() if now is None else now)


class ClientState(BaseModel):
    """
    Everything a :class:`~walletconnect.sign.SignClient` needs to resume after a restart.

    Serialize with :meth:`model_dump_json` / :meth:`model_validate_json` (or the
    :meth:`save` / :meth:`load` helpers, which take an explicit path).

    .. warning:: Contains secret key material. See the module docs.
    """

    model_config = ConfigDict(extra="ignore")

    version: Literal[1] = STATE_VERSION

    relay_key: str = Field(default_factory=_new_relay_key, repr=False)
    """Ed25519 seed (hex) for the relay identity (``did:key`` client ID). **Secret.**"""

    sessions: dict[str, Session] = Field(default_factory=dict)
    """Settled sessions keyed by session topic."""

    @field_validator("relay_key")
    @classmethod
    def _validate_relay_key(cls, value: str) -> str:
        return _check_hex32(value, "relay_key")

    @property
    def auth_key(self) -> AuthKeyPair:
        """The relay identity key pair derived from :attr:`relay_key`."""
        return generate_auth_key_pair(self.relay_key)

    @property
    def client_id(self) -> str:
        """The relay ``did:key`` this state authenticates as."""
        return self.auth_key.did_key

    def prune_expired(self, now: float | None = None) -> list[str]:
        """Drop expired sessions in place; return the removed topics."""
        expired = [t for t, s in self.sessions.items() if s.is_expired(now)]
        for topic in expired:
            del self.sessions[topic]
        return expired

    def save(self, path: str | os.PathLike[str]) -> Path:
        """
        Atomically write the state as JSON to ``path`` with ``0600`` permissions.

        The library has no default location: you choose ``path``. Missing parent
        directories are created with ``0700`` permissions.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = self.model_dump_json(indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            # mkstemp already creates the file 0600; chmod again for umask-proof clarity.
            os.chmod(tmp_name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
        return target

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> ClientState:
        """Read state previously written by :meth:`save` (or any ``model_dump_json``)."""
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


__all__ = ["STATE_VERSION", "ClientState", "Session"]
