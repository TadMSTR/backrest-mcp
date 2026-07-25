"""
Tests for BackrestClient — HTTP mechanics, auth, error handling.
"""

from __future__ import annotations

import base64
import json
import struct

import httpx
import pytest
import respx

from backrest_mcp.client import BackrestClient, BackrestStreamError, get_client

BASE_URL = "http://localhost:9898"


def _frame(payload: bytes, end=False):
    flag = 0b00000010 if end else 0
    return struct.pack(">BI", flag, len(payload)) + payload


def _bytesvalue_frame(text: str):
    return _frame(json.dumps({"value": base64.b64encode(text.encode()).decode()}).encode())


@pytest.fixture(autouse=True)
def reset_client_cache():
    """Reset lru_cache between tests."""
    get_client.cache_clear()
    yield
    get_client.cache_clear()


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("BACKREST_URL", BASE_URL)
    monkeypatch.setenv("BACKREST_USERNAME", "")
    monkeypatch.setenv("BACKREST_PASSWORD", "")


@pytest.fixture
def mock_env_auth(monkeypatch):
    monkeypatch.setenv("BACKREST_URL", BASE_URL)
    monkeypatch.setenv("BACKREST_USERNAME", "admin")
    monkeypatch.setenv("BACKREST_PASSWORD", "secret")


async def test_post_happy_path(mock_env):
    """Successful POST returns parsed JSON body."""
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json={"repos": [], "plans": []})
        )
        client = BackrestClient(BASE_URL)
        result = await client.post("GetConfig", {})
    assert result == {"repos": [], "plans": []}


async def test_post_http_error_raises():
    """4xx response raises httpx.HTTPStatusError."""
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        client = BackrestClient(BASE_URL)
        with pytest.raises(httpx.HTTPStatusError):
            await client.post("GetConfig", {})


async def test_basic_auth_header_sent(mock_env_auth):
    """Basic Auth header is included when credentials are set."""
    with respx.mock() as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json={})
        )
        client = BackrestClient(BASE_URL, username="admin", password="secret")
        await client.post("GetConfig", {})
        request = route.calls[0].request
        assert "Authorization" in request.headers
        assert request.headers["Authorization"].startswith("Basic ")


async def test_trailing_slash_stripped():
    """Base URL trailing slash is normalised."""
    client = BackrestClient("http://localhost:9898/")
    assert client._base == "http://localhost:9898"


async def test_no_auth_when_no_credentials():
    """No Authorization header when username/password are empty."""
    with respx.mock() as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/GetConfig").mock(
            return_value=httpx.Response(200, json={})
        )
        client = BackrestClient(BASE_URL)
        await client.post("GetConfig", {})
        request = route.calls[0].request
        assert "Authorization" not in request.headers


# ---------------------------------------------------------------------------
# Connect server-streaming (GetLogs)
# ---------------------------------------------------------------------------

async def test_post_streaming_decodes_and_concatenates():
    """Streaming RPC decodes base64 BytesValue frames and concatenates payloads."""
    stream = _bytesvalue_frame("hello ") + _bytesvalue_frame("world") + _frame(b"{}", end=True)
    with respx.mock() as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/GetLogs").mock(
            return_value=httpx.Response(200, content=stream)
        )
        client = BackrestClient(BASE_URL)
        out = await client.post_streaming("GetLogs", {"ref": "t-1"})
    assert out == b"hello world"
    # Streaming uses the Connect envelope content type.
    assert route.calls[0].request.headers["content-type"] == "application/connect+json"


async def test_post_streaming_error_frame_raises():
    """An end-of-stream frame carrying an error raises BackrestStreamError."""
    err = json.dumps({"error": {"code": "not_found", "message": "log expired"}}).encode()
    stream = _frame(err, end=True)
    with respx.mock() as mock:
        mock.post(f"{BASE_URL}/v1.Backrest/GetLogs").mock(
            return_value=httpx.Response(200, content=stream)
        )
        client = BackrestClient(BASE_URL)
        with pytest.raises(BackrestStreamError, match="log expired"):
            await client.post_streaming("GetLogs", {"ref": "t-expired"})


async def test_post_streaming_request_is_enveloped():
    """The streaming request body is a length-prefixed Connect envelope."""
    with respx.mock() as mock:
        route = mock.post(f"{BASE_URL}/v1.Backrest/GetLogs").mock(
            return_value=httpx.Response(200, content=_frame(b"{}", end=True))
        )
        client = BackrestClient(BASE_URL)
        await client.post_streaming("GetLogs", {"ref": "t-1"})
    body = route.calls[0].request.content
    flag, length = struct.unpack(">BI", body[:5])
    assert flag == 0
    assert length == len(body) - 5
    assert json.loads(body[5:]) == {"ref": "t-1"}
