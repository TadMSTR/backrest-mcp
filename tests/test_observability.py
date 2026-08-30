"""
Observability-layer tests — the telemetry code that was failing silently in
production (vikunja#575 item 3, #580). Ported from the dockhand-mcp reference
implementation (TadMSTR/dockhand-mcp#6, `42f73cf`).

Four things are asserted here that a code read cannot establish:

1. A *configured but failing* backend warns exactly once and is then not retried.
   Both halves of the NATS flood are covered: the old `except Exception: pass`
   left the client global at ``None`` so every ``emit_metric()`` re-entered the
   connect path (fixed by the sentinel), and nats-py's own defaults meant a
   *single* ``connect()`` retried 60 times, two seconds apart, reporting each
   attempt (fixed by ``_NATS_CONNECT_OPTS`` and the replacement ``error_cb``).
   The sentinel alone leaves the flood in place.
2. The **write/publish** path warns too. ``InfluxDBClient3`` constructs lazily and
   never contacts the host, so on a bad URL ``_get_influx()`` *succeeds* and the
   init sentinel never fires — the failure only ever surfaces on ``write()``.
   Without this half the InfluxDB fix is a no-op (#580).
3. The warning never carries the URL or the token. A NATS URL is
   ``nats://user:password@host``; leaking it into a log this build is otherwise
   making quieter would be a straight downgrade.
4. ``configure_logging()`` attaches exactly one sink, and third-party loggers do
   not inherit ``LOG_LEVEL`` (51,905 third-party lines to 6 of ours, measured).

The optional backends (``influxdb_client_3``, ``nats-py``) are deliberately faked
via ``sys.modules`` rather than imported: CI installs only ``.[dev]``, so a test
that needed the real package would be skipped exactly where it matters most.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types

import pytest

from backrest_mcp import observability

# A stand-in for a real credential-bearing URL. Every failure-path test asserts
# this string reaches no log payload — an assertion that fails loudly if someone
# later "improves" the warning by adding str(exc) or the config back in.
SECRET_URL = "nats://agent-backrest:sup3rs3cr3t-pw@nats.internal:4222"
SECRET_TOKEN = "influx-token-do-not-log-me"


class _RecordingLogger:
    """Minimal stand-in for the module's structlog logger.

    Captures ``(event, kwargs)`` so a test can assert both *that* a warning fired
    and *what* was in it. Asserting the payload is the point: "no secret in the
    log" passes trivially when nothing is logged at all, so every leak test also
    asserts the event fired and carried an ``error_class``.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event, **kw):
        self.warnings.append((event, kw))

    def info(self, event, **kw):  # pragma: no cover - not asserted on
        pass

    def payload_text(self) -> str:
        return repr(self.warnings)


@pytest.fixture
def rec_log(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(observability, "log", recorder)
    return recorder


@pytest.fixture(autouse=True)
def reset_observability_globals(monkeypatch):
    """Reset the backend globals around every test.

    They are module-level negative caches by design — without this, the first
    test to trip a sentinel would disable the backend for the whole session and
    every later assertion would pass for the wrong reason.
    """
    for name, value in (
        ("_influx_client", None),
        ("_influx_failed", False),
        ("_influx_write_failed_logged", False),
        ("_nats_client", None),
        ("_nats_failed", False),
        ("_nats_publish_failed_logged", False),
        ("_nats_error_logged", False),
    ):
        monkeypatch.setattr(observability, name, value)
    yield


@pytest.fixture(autouse=True)
def restore_logging():
    """Snapshot and restore global logging state.

    ``configure_logging()`` clears the root handlers; leaving that in place would
    break pytest's own capture for every test that ran afterwards.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_third_party = {
        name: logging.getLogger(name).level for name in observability._THIRD_PARTY_LOGGERS
    }
    yield
    for h in root.handlers:
        if h not in saved_handlers:
            h.close()
    root.handlers.clear()
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    for name, level in saved_third_party.items():
        logging.getLogger(name).setLevel(level)


def _fake_influx_module(*, raise_on_init=None, client=None):
    """Build a fake ``influxdb_client_3`` module for ``sys.modules``."""
    mod = types.ModuleType("influxdb_client_3")

    def _ctor(**kwargs):
        if raise_on_init is not None:
            raise raise_on_init
        return client

    class _Point:
        def __init__(self, measurement):
            self.measurement = measurement

        def tag(self, k, v):
            return self

        def field(self, k, v):
            return self

    mod.InfluxDBClient3 = _ctor
    mod.Point = _Point
    return mod


# ---------------------------------------------------------------------------
# configure_logging — one sink, and third-party loggers demoted
# ---------------------------------------------------------------------------

def test_configure_logging_sets_level(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("LOG_FILE", raising=False)
    observability.configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_writes_to_file(monkeypatch, tmp_path):
    log_file = tmp_path / "logs" / "backrest.log"
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("LOG_FILE", str(log_file))
    observability.configure_logging()
    assert log_file.parent.exists()


def test_configure_logging_attaches_only_the_file_handler(tmp_path, monkeypatch):
    """Item 5: one sink, not two.

    A stderr StreamHandler *and* a FileHandler were both attached, so under PM2
    every line landed in `~/.pm2/logs/backrest-mcp-error.log` *and* in LOG_FILE —
    two near-identical unrotated files (8.5 MB + 9.5 MB, measured 2026-08-30).
    """
    log_file = tmp_path / "logs" / "backrest-mcp.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    observability.configure_logging()

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.FileHandler)
    assert not any(
        type(h) is logging.StreamHandler for h in handlers
    ), "stderr handler must be dropped when LOG_FILE is writable"


def test_configure_logging_falls_back_to_stderr_when_log_file_unwritable(
    tmp_path, monkeypatch, capsys
):
    """The OSError fallback must survive item 5 — it is what keeps CI alive."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("regular file, so makedirs below it raises NotADirectoryError")
    monkeypatch.setenv("LOG_FILE", str(blocker / "sub" / "backrest-mcp.log"))

    observability.configure_logging()

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert type(handlers[0]) is logging.StreamHandler
    assert handlers[0].stream is sys.stderr
    assert "file logging disabled" in capsys.readouterr().err


def test_configure_logging_with_log_file_unset_uses_stderr(monkeypatch):
    monkeypatch.setenv("LOG_FILE", "")

    observability.configure_logging()

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert type(handlers[0]) is logging.StreamHandler


def test_third_party_loggers_do_not_inherit_log_level(tmp_path, monkeypatch):
    """Item 4: httpx/httpcore/mcp/nats stay at WARNING even at LOG_LEVEL=DEBUG.

    DEBUG is the case that matters — at INFO the assertion would also pass if the
    demotion were missing but the root happened to sit above the library's level.
    """
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "backrest-mcp.log"))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    observability.configure_logging()

    assert logging.getLogger().level == logging.DEBUG
    for name in ("httpx", "httpcore", "mcp", "nats"):
        assert logging.getLogger(name).level == logging.WARNING, name
    # Children inherit the demotion through normal propagation — these are the
    # loggers that actually emitted the measured noise (21,088 + 10,282 + 10,273
    # + 10,262 lines against 6 of ours).
    for child in ("httpcore.http11", "mcp.server.lowlevel", "nats.aio.client"):
        assert logging.getLogger(child).getEffectiveLevel() == logging.WARNING, child
    # The app's own logger is untouched and still honours LOG_LEVEL.
    assert logging.getLogger("backrest_mcp.server").getEffectiveLevel() == logging.DEBUG


def test_third_party_logger_list_covers_the_measured_noise():
    """Guards the closed set. Dropping a name here is a silent regression."""
    assert set(observability._THIRD_PARTY_LOGGERS) == {"httpx", "httpcore", "mcp", "nats"}


# ---------------------------------------------------------------------------
# Item 1 — InfluxDB sentinel
# ---------------------------------------------------------------------------

def test_get_influx_unset_env_is_silently_disabled(monkeypatch, rec_log):
    """A *missing* env var is the intended disabled path and must not warn."""
    monkeypatch.delenv("INFLUXDB_URL", raising=False)

    assert observability._get_influx() is None
    assert rec_log.warnings == []
    assert observability._influx_failed is False, "never-tried must stay distinct from failed"


def test_get_influx_failure_warns_once_and_is_not_retried(monkeypatch, rec_log):
    calls = []

    def _ctor(**kwargs):
        calls.append(kwargs)
        raise ConnectionRefusedError("connection refused")

    mod = _fake_influx_module()
    mod.InfluxDBClient3 = _ctor
    monkeypatch.setitem(sys.modules, "influxdb_client_3", mod)
    monkeypatch.setenv("INFLUXDB_URL", "http://127.0.0.1:9/unreachable")
    monkeypatch.setenv("INFLUXDB_TOKEN", SECRET_TOKEN)

    assert observability._get_influx() is None
    assert observability._get_influx() is None
    assert observability._get_influx() is None

    # The negative cache is the fix for the retry storm: one attempt, not three.
    assert len(calls) == 1
    assert observability._influx_failed is True
    assert len(rec_log.warnings) == 1
    event, kw = rec_log.warnings[0]
    assert event == "influx_init_failed"
    assert kw["error_class"] == "ConnectionRefusedError"


def test_get_influx_failure_warning_carries_no_credentials(monkeypatch, rec_log):
    """The one place this build could introduce a leak."""
    mod = _fake_influx_module(
        raise_on_init=ValueError(f"bad host {SECRET_URL} token {SECRET_TOKEN}")
    )
    monkeypatch.setitem(sys.modules, "influxdb_client_3", mod)
    monkeypatch.setenv("INFLUXDB_URL", SECRET_URL)
    monkeypatch.setenv("INFLUXDB_TOKEN", SECRET_TOKEN)

    observability._get_influx()

    # Assert the warning fired *and* is clean — "no secret in the log" would pass
    # trivially against a code path that logs nothing at all.
    assert [e for e, _ in rec_log.warnings] == ["influx_init_failed"]
    assert rec_log.warnings[0][1]["error_class"] == "ValueError"
    text = rec_log.payload_text()
    assert SECRET_URL not in text
    assert SECRET_TOKEN not in text
    assert "sup3rs3cr3t-pw" not in text


def test_get_influx_success_is_cached(monkeypatch, rec_log):
    sentinel = object()
    calls = []

    def _ctor(**kwargs):
        calls.append(kwargs)
        return sentinel

    mod = _fake_influx_module()
    mod.InfluxDBClient3 = _ctor
    monkeypatch.setitem(sys.modules, "influxdb_client_3", mod)
    monkeypatch.setenv("INFLUXDB_URL", "http://127.0.0.1:8182")
    monkeypatch.setenv("INFLUXDB_BUCKET", "forge")

    assert observability._get_influx() is sentinel
    assert observability._get_influx() is sentinel
    assert len(calls) == 1
    assert calls[0]["database"] == "forge"
    assert rec_log.warnings == []


# ---------------------------------------------------------------------------
# Item 1 — NATS sentinel
# ---------------------------------------------------------------------------

async def test_get_nats_unset_env_is_silently_disabled(monkeypatch, rec_log):
    monkeypatch.delenv("NATS_URL", raising=False)

    assert await observability._get_nats() is None
    assert rec_log.warnings == []
    assert observability._nats_failed is False


async def test_get_nats_failure_warns_once_and_is_not_retried(monkeypatch, rec_log):
    """This is the flood bug: every emit_metric() re-entered nats.connect()."""
    connects = []

    async def _connect(url, **kwargs):
        connects.append(url)
        raise OSError("nats: 'Authorization Violation'")

    mod = types.ModuleType("nats")
    mod.connect = _connect
    monkeypatch.setitem(sys.modules, "nats", mod)
    monkeypatch.setenv("NATS_URL", SECRET_URL)

    for _ in range(5):
        assert await observability._get_nats() is None

    assert len(connects) == 1, "a failed NATS backend must not be reconnected per call"
    assert observability._nats_failed is True
    assert [e for e, _ in rec_log.warnings] == ["nats_init_failed"]
    assert rec_log.warnings[0][1]["error_class"] == "OSError"
    # A NATS URL embeds its credentials; neither it nor str(exc) may be logged.
    text = rec_log.payload_text()
    assert SECRET_URL not in text
    assert "sup3rs3cr3t-pw" not in text
    assert "Authorization Violation" not in text


# ---------------------------------------------------------------------------
# Item 3 — write/publish failures are warned once, not swallowed forever
# ---------------------------------------------------------------------------

async def test_emit_metric_write_failure_warns_once(monkeypatch, rec_log):
    """The half that must not be dropped (#580).

    ``InfluxDBClient3`` builds lazily, so a bad ``INFLUXDB_URL`` never fails at
    init — ``_get_influx()`` returns a client and only ``write()`` raises. An
    init-only sentinel leaves a misconfigured InfluxDB exactly as silent as it
    was for 35 days.
    """

    class _Client:
        def __init__(self):
            self.writes = 0

        def write(self, record=None):
            self.writes += 1
            raise RuntimeError(f"write rejected for {SECRET_TOKEN}")

    client = _Client()
    monkeypatch.setitem(sys.modules, "influxdb_client_3", _fake_influx_module(client=client))
    monkeypatch.setattr(observability, "_influx_client", client)
    monkeypatch.delenv("NATS_URL", raising=False)

    for _ in range(3):
        await observability.emit_metric("backrest_tool", {"tool": "t"}, {"duration_ms": 1.0})

    # Unlike an init failure, a write failure is often transient — keep writing,
    # but log only the first so a broken collector cannot flood the file.
    assert client.writes == 3
    assert [e for e, _ in rec_log.warnings] == ["influx_write_failed"]
    assert rec_log.warnings[0][1]["error_class"] == "RuntimeError"
    assert SECRET_TOKEN not in rec_log.payload_text()


async def test_emit_metric_publish_failure_warns_once(monkeypatch, rec_log):
    class _NatsClient:
        def __init__(self):
            self.publishes = 0

        async def publish(self, subject, payload):
            self.publishes += 1
            raise OSError(f"publish rejected by {SECRET_URL}")

    client = _NatsClient()
    monkeypatch.setattr(observability, "_nats_client", client)
    monkeypatch.delenv("INFLUXDB_URL", raising=False)

    for _ in range(3):
        await observability.emit_metric("backrest_tool", {"tool": "t"}, {"duration_ms": 1.0})

    assert client.publishes == 3
    assert [e for e, _ in rec_log.warnings] == ["nats_publish_failed"]
    assert rec_log.warnings[0][1]["error_class"] == "OSError"
    text = rec_log.payload_text()
    assert SECRET_URL not in text
    assert "sup3rs3cr3t-pw" not in text


async def test_emit_metric_noop_without_backends(monkeypatch, rec_log):
    """emit_metric is a no-op (no exception) when no telemetry backend is configured."""
    monkeypatch.delenv("INFLUXDB_URL", raising=False)
    monkeypatch.delenv("NATS_URL", raising=False)

    await observability.emit_metric("backrest_tool", {"tool": "test"}, {"duration_ms": 1.0})

    assert rec_log.warnings == []


async def test_emit_metric_writes_a_point_and_publishes(monkeypatch, rec_log):
    written = []
    published = []

    class _Client:
        def write(self, record=None):
            written.append(record)

    class _NatsClient:
        async def publish(self, subject, payload):
            published.append((subject, payload))

    monkeypatch.setitem(sys.modules, "influxdb_client_3", _fake_influx_module())
    monkeypatch.setattr(observability, "_influx_client", _Client())
    monkeypatch.setattr(observability, "_nats_client", _NatsClient())
    monkeypatch.setenv("NATS_SUBJECT_PREFIX", "backrest")

    await observability.emit_metric(
        "backrest_tool", {"tool": "get_health"}, {"duration_ms": 0.5}
    )

    assert len(written) == 1
    assert published[0][0] == "backrest.tool.get_health"
    assert b'"measurement": "backrest_tool"' in published[0][1]
    assert rec_log.warnings == []


# ---------------------------------------------------------------------------
# Item 2 — nats-py's own reconnect machinery, the larger half of the flood
# ---------------------------------------------------------------------------

def test_nats_connect_options_are_fail_fast():
    """The library defaults (60 attempts x 2 s, reporting each one) are what made
    a single connect() block a tool call for ~120 s and log ~60 times."""
    opts = observability._NATS_CONNECT_OPTS
    assert opts["allow_reconnect"] is False
    # 0 is a trap, not "no retries": _select_next_server() only discards a server
    # when `max_reconnect_attempts > 0`, so 0 retries forever.
    assert opts["max_reconnect_attempts"] > 0
    assert opts["max_reconnect_attempts"] <= 2
    assert opts["connect_timeout"] <= 5
    assert observability._NATS_CONNECT_DEADLINE <= 10


async def test_get_nats_passes_fail_fast_options_and_our_error_cb(monkeypatch, rec_log):
    seen = {}

    async def _connect(url, **kwargs):
        seen.update(kwargs)
        raise OSError("refused")

    mod = types.ModuleType("nats")
    mod.connect = _connect
    monkeypatch.setitem(sys.modules, "nats", mod)
    monkeypatch.setenv("NATS_URL", SECRET_URL)

    await observability._get_nats()

    assert seen["allow_reconnect"] is False
    assert (
        seen["max_reconnect_attempts"]
        == observability._NATS_CONNECT_OPTS["max_reconnect_attempts"]
    )
    # Without our own error_cb, nats-py's default logs at ERROR on the
    # `nats.aio.client` logger — which the WARNING demotion does not silence.
    assert seen["error_cb"] is observability._nats_error_cb


async def test_get_nats_is_bounded_by_a_deadline(monkeypatch, rec_log):
    """Belt and braces: whatever the library does internally, a dead NATS may not
    hang the tool call that triggered the metric."""
    monkeypatch.setattr(observability, "_NATS_CONNECT_DEADLINE", 0.05)

    async def _hang(url, **kwargs):
        await asyncio.sleep(30)

    mod = types.ModuleType("nats")
    mod.connect = _hang
    monkeypatch.setitem(sys.modules, "nats", mod)
    monkeypatch.setenv("NATS_URL", SECRET_URL)

    result = await asyncio.wait_for(observability._get_nats(), timeout=5)

    assert result is None
    assert observability._nats_failed is True
    assert [e for e, _ in rec_log.warnings] == ["nats_init_failed"]
    assert rec_log.warnings[0][1]["error_class"] == "TimeoutError"


async def test_nats_error_cb_warns_once_and_leaks_nothing(rec_log):
    """This callback replaces the one that produces the per-attempt ERROR lines."""
    for _ in range(50):
        await observability._nats_error_cb(
            ConnectionRefusedError(f"cannot reach {SECRET_URL}")
        )

    assert [e for e, _ in rec_log.warnings] == ["nats_transport_error"]
    assert rec_log.warnings[0][1]["error_class"] == "ConnectionRefusedError"
    text = rec_log.payload_text()
    assert SECRET_URL not in text
    assert "sup3rs3cr3t-pw" not in text


def test_nats_logger_demotion_does_not_cover_the_default_error_cb():
    """Documents *why* the error_cb replacement is needed on top of item 4.

    nats-py logs `nats: encountered error` at ERROR. Demoting the `nats` logger to
    WARNING lets ERROR through, so the demotion alone would not have stopped the
    flood — an easy and wrong assumption to make from the ticket.
    """
    assert logging.ERROR > logging.WARNING


def test_no_log_site_renders_an_exception_traceback():
    """Pins the audit split from dockhand-mcp (2026-08-29, LOW-1).

    There is where that build kept two `exc_info=True` sites: both on OTel, whose
    config carries no credential. backrest-mcp has no OTel path at all, so every
    log site here belongs to a credential-bearing backend (a NATS URL is
    `nats://user:password@host`) and the exempt set is empty. A new `exc_info=True`
    anywhere in this module breaks this test on purpose.
    """
    import pathlib

    src = pathlib.Path(observability.__file__).read_text()
    offenders = [ln.strip() for ln in src.splitlines() if "exc_info=True" in ln]
    assert offenders == [], offenders
