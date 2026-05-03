"""Core: remote atomic Houdini operations (TCP stubs; implementation lives in Houdini receiver)."""

from core.bridge import BRIDGE_VERSION, BridgeError, send_expect_core_result, send_raw
from core.result import CoreResult

__all__ = [
    "BRIDGE_VERSION",
    "BridgeError",
    "CoreResult",
    "send_expect_core_result",
    "send_raw",
]
