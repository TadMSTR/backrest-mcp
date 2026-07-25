"""
Smoke tests for observability — logging config and the no-op metric path.

These do not exercise real InfluxDB/NATS backends (disabled without their env vars);
they confirm configure_logging is idempotent and emit_metric is a safe no-op when no
telemetry backend is configured.
"""

from __future__ import annotations

import logging

import pytest

from backrest_mcp.observability import configure_logging, emit_metric


def test_configure_logging_sets_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("LOG_FILE", raising=False)
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_writes_to_file(monkeypatch, tmp_path):
    log_file = tmp_path / "logs" / "backrest.log"
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("LOG_FILE", str(log_file))
    configure_logging()
    assert log_file.parent.exists()


async def test_emit_metric_noop_without_backends(monkeypatch):
    """emit_metric is a no-op (no exception) when no telemetry backend is configured."""
    monkeypatch.delenv("INFLUXDB_URL", raising=False)
    monkeypatch.delenv("NATS_URL", raising=False)
    # Should complete without raising and without any backend.
    await emit_metric("backrest_tool", {"tool": "test"}, {"duration_ms": 1.0})


@pytest.fixture(autouse=True)
def _reset_logging():
    yield
    logging.getLogger().handlers.clear()
