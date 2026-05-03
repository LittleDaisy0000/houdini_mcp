"""TCP JSON bridge to the in-Houdini receiver (same line-delimited style as mobu_mcp_server)."""

from __future__ import annotations

import json
import os
import socket
import uuid
from typing import Any

from core.result import CoreResult

BRIDGE_VERSION = "houdini-mcp-bridge-sprint2-p72"


class BridgeError(RuntimeError):
    pass


HOUDINI_HOST = os.getenv("HOUDINI_HOST", "127.0.0.1")
HOUDINI_PORT = int(os.getenv("HOUDINI_PORT", "63556"))
# Per-socket read/write timeout (large flipbook / base64 / heavy cook responses).
HOUDINI_TIMEOUT_SEC = float(os.getenv("HOUDINI_SOCKET_TIMEOUT_SEC") or os.getenv("HOUDINI_TIMEOUT_SEC", "30"))
# TCP connect() timeout; defaults to same as socket timeout if unset.
HOUDINI_CONNECT_TIMEOUT_SEC = float(os.getenv("HOUDINI_CONNECT_TIMEOUT_SEC") or str(HOUDINI_TIMEOUT_SEC))


def _decode_line(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_first_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise BridgeError(f"Receiver response is not valid JSON text: {text[:200]!r}")
    return text[start : end + 1]


def send_raw(tool: str, args: dict[str, Any]) -> Any:
    """Send one request line; return parsed top-level response dict (ok, result, error, ...)."""
    req = {
        "request_id": str(uuid.uuid4()),
        "tool": tool,
        "args": args,
        "meta": {"source": "houdini-mcp-bridge", "bridge_version": BRIDGE_VERSION},
    }
    try:
        with socket.create_connection((HOUDINI_HOST, HOUDINI_PORT), timeout=HOUDINI_CONNECT_TIMEOUT_SEC) as conn:
            conn.settimeout(HOUDINI_TIMEOUT_SEC)
            payload = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
            conn.sendall(payload)
            raw = b""
            while b"\n" not in raw:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
        if not raw:
            raise BridgeError("No response from Houdini receiver")
        line = raw.split(b"\n", 1)[0]
        text = _extract_first_json_object(_decode_line(line))
        return json.loads(text)
    except TimeoutError as e:
        raise BridgeError("Timeout talking to Houdini receiver") from e
    except OSError as e:
        raise BridgeError(f"Cannot connect to Houdini receiver at {HOUDINI_HOST}:{HOUDINI_PORT}") from e


def _looks_like_core_envelope(d: Any) -> bool:
    """Receiver puts batch/core outcomes in ``result`` as ``{ok, ...}`` (always includes ``ok``)."""
    return isinstance(d, dict) and "ok" in d


def send_expect_core_result(tool: str, args: dict[str, Any]) -> CoreResult:
    """Call receiver; expect result envelope {ok,data,warnings,errors} inside resp['result']."""
    resp = send_raw(tool, args)
    top_ok = bool(resp.get("ok", False))
    inner = resp.get("result")

    if _looks_like_core_envelope(inner):
        cr = CoreResult.from_dict(inner)
        if not top_ok and not cr.errors:
            err = resp.get("error") or {}
            msg = err.get("message", "Unknown error")
            code = err.get("code", "BRIDGE_ERROR")
            cr.errors = [f"{code}: {msg}"]
        return cr

    if not top_ok:
        err = resp.get("error") or {}
        msg = err.get("message", "Unknown error")
        code = err.get("code", "BRIDGE_ERROR")
        return CoreResult(ok=False, errors=[f"{code}: {msg}"])

    return CoreResult(ok=True, data=inner)
