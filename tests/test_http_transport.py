"""
HTTP transport / fail-closed guard tests.

The stdio transport has no network surface; the http transport (the long-lived PM2
service) does, so main() must fail closed: loopback-only bind and a sufficiently long
bearer token, or it refuses to start.
"""

from __future__ import annotations

import sys

import pytest

TOKEN = "a" * 64  # 64-char token, above _MIN_AUTH_TOKEN_LENGTH


def _reload_server(monkeypatch, **env):
    monkeypatch.setenv("BACKREST_URL", "http://localhost:9898")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for mod in ["backrest_mcp.safety", "backrest_mcp.server"]:
        if mod in sys.modules:
            del sys.modules[mod]
    import backrest_mcp.server as srv
    return srv


@pytest.fixture(autouse=True)
def reset_client_cache():
    from backrest_mcp.client import get_client
    get_client.cache_clear()
    yield
    get_client.cache_clear()


def test_stdio_default_needs_no_auth(monkeypatch):
    srv = _reload_server(monkeypatch)
    assert srv.TRANSPORT == "stdio"
    assert srv._auth is None


def test_auth_verifier_configured_when_token_set(monkeypatch):
    srv = _reload_server(monkeypatch, BACKREST_MCP_AUTH_TOKEN=TOKEN)
    assert srv._auth is not None


def test_http_without_token_refuses(monkeypatch):
    srv = _reload_server(monkeypatch, BACKREST_MCP_TRANSPORT="http")
    with pytest.raises(RuntimeError, match="without BACKREST_MCP_AUTH_TOKEN"):
        srv.main()


def test_http_short_token_refuses(monkeypatch):
    srv = _reload_server(monkeypatch, BACKREST_MCP_TRANSPORT="http", BACKREST_MCP_AUTH_TOKEN="short")
    with pytest.raises(RuntimeError, match="too short"):
        srv.main()


def test_http_nonloopback_refuses(monkeypatch):
    srv = _reload_server(
        monkeypatch,
        BACKREST_MCP_TRANSPORT="http",
        BACKREST_MCP_HTTP_HOST="0.0.0.0",
        BACKREST_MCP_AUTH_TOKEN=TOKEN,
    )
    with pytest.raises(RuntimeError, match="non-loopback"):
        srv.main()


def test_http_valid_config_runs(monkeypatch):
    srv = _reload_server(
        monkeypatch,
        BACKREST_MCP_TRANSPORT="http",
        BACKREST_MCP_HTTP_PORT="8626",
        BACKREST_MCP_AUTH_TOKEN=TOKEN,
    )
    calls = {}
    monkeypatch.setattr(srv.mcp, "run", lambda **kw: calls.update(kw))
    srv.main()
    assert calls["transport"] == "http"
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8626


def test_nonloopback_allowed_with_override(monkeypatch):
    srv = _reload_server(
        monkeypatch,
        BACKREST_MCP_TRANSPORT="http",
        BACKREST_MCP_HTTP_HOST="0.0.0.0",
        BACKREST_MCP_ALLOW_NONLOOPBACK="1",
        BACKREST_MCP_AUTH_TOKEN=TOKEN,
    )
    calls = {}
    monkeypatch.setattr(srv.mcp, "run", lambda **kw: calls.update(kw))
    srv.main()
    assert calls["host"] == "0.0.0.0"
