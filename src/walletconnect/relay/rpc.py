"""JSON-RPC helpers for the WalletConnect ``irn`` relay."""

from __future__ import annotations

from typing import Any

from walletconnect.exceptions import RelayError
from walletconnect.utils.ids import payload_id


class RelayRPCError(RelayError):
    """A JSON-RPC error object returned by the relay."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"Relay JSON-RPC error [{code}]: {message}")


def make_request(method: str, params: dict[str, Any] | list[Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 request with a WalletConnect-style 19-digit id."""
    request: dict[str, Any] = {
        "id": payload_id(),
        "jsonrpc": "2.0",
        "method": method,
    }
    if params is not None:
        request["params"] = params
    return request


def make_result(request_id: int | str, result: Any = True) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response (used to ack ``irn_subscription``)."""
    return {"id": request_id, "jsonrpc": "2.0", "result": result}


def make_error(
    request_id: int | str | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    response: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if request_id is not None:
        response["id"] = request_id
    return response
