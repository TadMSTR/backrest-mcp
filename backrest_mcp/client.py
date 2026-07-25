"""
Backrest HTTP client — connect-rpc-over-HTTP.

Unary RPCs use the Connect unary protocol (plain POST + JSON body):
  POST {base_url}/v1.Backrest/{MethodName}

Server-streaming RPCs (e.g. GetLogs) use the Connect streaming protocol:
  POST with content-type application/connect+json and an enveloped request frame;
  the response is a sequence of enveloped frames terminated by an end-of-stream frame.

Optional Basic Auth via BACKREST_USERNAME / BACKREST_PASSWORD env vars.
4xx/5xx responses raise httpx.HTTPStatusError.
"""

from __future__ import annotations

import base64
import json
import os
import struct
from functools import lru_cache

import httpx
import structlog

log = structlog.get_logger(__name__)

# Connect streaming envelope flags (1-byte prefix on each frame).
_FLAG_END_STREAM = 0b00000010


class BackrestStreamError(RuntimeError):
    """Raised when a Connect streaming RPC ends with an error frame."""


class BackrestClient:
    """Async HTTP client for the Backrest connect-rpc API."""

    def __init__(self, base_url: str, username: str = "", password: str = "") -> None:
        self._base = base_url.rstrip("/")
        self._auth = (username, password) if username and password else None

    async def post(self, method: str, body: dict) -> dict:
        """Call a unary Connect RPC and return the decoded JSON response."""
        url = f"{self._base}/v1.Backrest/{method}"
        log.debug("backrest_request", method=method)
        async with httpx.AsyncClient(auth=self._auth, timeout=120.0) as client:
            r = await client.post(url, json=body)
            r.raise_for_status()
            return r.json()

    async def post_streaming(self, method: str, body: dict) -> bytes:
        """Call a server-streaming Connect RPC and return the concatenated payload bytes.

        Backrest's streaming RPCs used here (GetLogs) stream `types.BytesValue` frames,
        each JSON-encoded as {"value": "<base64>"}. This decodes and concatenates the
        base64 payloads across all data frames.

        Raises BackrestStreamError if the stream terminates with an error frame.
        """
        url = f"{self._base}/v1.Backrest/{method}"
        log.debug("backrest_stream_request", method=method)
        msg = json.dumps(body).encode()
        envelope = struct.pack(">BI", 0, len(msg)) + msg
        async with httpx.AsyncClient(auth=self._auth, timeout=120.0) as client:
            r = await client.post(
                url,
                content=envelope,
                headers={"content-type": "application/connect+json"},
            )
            r.raise_for_status()
            data = r.content
        return _decode_connect_stream(data)


def _decode_connect_stream(data: bytes) -> bytes:
    """Parse a buffered Connect streaming response into concatenated BytesValue payloads."""
    out = bytearray()
    i = 0
    n = len(data)
    while i + 5 <= n:
        flag = data[i]
        length = struct.unpack(">I", data[i + 1 : i + 5])[0]
        frame = data[i + 5 : i + 5 + length]
        i += 5 + length
        if flag & _FLAG_END_STREAM:
            # End-of-stream frame: JSON object, non-empty "error" means the RPC failed.
            try:
                end = json.loads(frame) if frame else {}
            except json.JSONDecodeError:
                end = {}
            err = end.get("error")
            if err:
                raise BackrestStreamError(err.get("message") or str(err))
            break
        try:
            payload = json.loads(frame)
        except json.JSONDecodeError:
            continue
        value = payload.get("value")
        if value:
            out += base64.b64decode(value)
    return bytes(out)


@lru_cache(maxsize=1)
def get_client() -> BackrestClient:
    return BackrestClient(
        base_url=os.environ.get("BACKREST_URL", "http://localhost:9898"),
        username=os.environ.get("BACKREST_USERNAME", ""),
        password=os.environ.get("BACKREST_PASSWORD", ""),
    )
