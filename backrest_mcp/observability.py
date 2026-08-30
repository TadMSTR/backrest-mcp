"""
Observability setup — structlog (always on) + optional InfluxDB/OTEL/NATS.

Each backend is gated on its env var. Missing env var = backend disabled.
No import errors if optional packages are absent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Library loggers demoted to WARNING by configure_logging(). Names are logger
# *prefixes* — `logging` applies the level to every child (`httpcore.http11`,
# `mcp.server.lowlevel`, `nats.aio.client`, ...) through normal propagation.
_THIRD_PARTY_LOGGERS = ("httpx", "httpcore", "mcp", "nats")


def configure_logging() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_file = os.environ.get("LOG_FILE", "")

    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    # ONE sink, not two. A stderr StreamHandler and a FileHandler were both
    # attached, so under PM2 every line was written to `error_file` *and* to
    # LOG_FILE — measured 2026-08-30 as two near-identical unrotated files,
    # /home/ted/logs/backrest-mcp.log (8.5 MB) and
    # ~/.pm2/logs/backrest-mcp-error.log (9.5 MB), per-logger counts matching
    # within ~500 lines. Same shape as vikunja#574 P4 and #552. The file handler
    # wins when LOG_FILE is writable; stderr is the fallback, which is what keeps
    # CI (and any restricted-perms runner) alive.
    handlers: list[logging.Handler] = []
    if log_file:
        try:
            log_dir = os.path.dirname(log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            handlers.append(logging.FileHandler(log_file))
        except OSError as exc:
            # An unwritable log path (CI runner, restricted perms) must not crash
            # startup — fall back to stderr-only logging.
            print(
                f"backrest-mcp: file logging disabled ({log_file}): {exc}",
                file=sys.stderr,
            )
    if not handlers:
        handlers.append(logging.StreamHandler(sys.stderr))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    for h in handlers:
        root_logger.addHandler(h)
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Third-party wire trace is not this service's log. The root logger sits at
    # LOG_LEVEL, so httpx/httpcore/mcp/nats all inherited INFO and drowned the
    # app's own lines. Measured in /home/ted/logs/backrest-mcp.log on 2026-08-30:
    # mcp.server.lowlevel.server 21,088; mcp.server.streamable_http_manager
    # 10,282; mcp.server.streamable_http 10,273; httpx 10,262 — against 6 lines
    # from __main__. Demote them to WARNING and leave the backrest_mcp logger at
    # LOG_LEVEL — same fix as dockhand-mcp (vikunja#574), task-dispatcher (#552)
    # and scoped-mcp (#554).
    #
    # This does NOT cover nats-py's reconnect reporting: its default error_cb logs
    # at ERROR on `nats.aio.client`, which a WARNING demotion lets straight
    # through. `_nats_error_cb` below is what handles that.
    for _noisy in _THIRD_PARTY_LOGGERS:
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )
    for h in handlers:
        h.setFormatter(formatter)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# A *configured but failing* backend must be visible and must not be retried on
# every tool call. `except Exception: pass` left the client global at None, which
# meant (a) logs with zero lines mentioning influx while the backend was down, and
# (b) every emit_metric() re-entering the connect path — for NATS that spawned a
# fresh allow_reconnect background loop per call (vikunja#575 item 3, #580).
#
# The sentinel is a distinct flag rather than an overloaded None so "never tried"
# and "tried and failed" stay distinguishable: a missing env var is the intended
# disabled path and must stay silent, a failed init must warn exactly once.
#
# SECURITY: the warning carries the exception *class*, never str(exc). A NATS URL
# is `nats://user:password@host` and an InfluxDB error can echo the host/token —
# both would land verbatim in a log this build is otherwise making quieter.
_influx_client = None
_influx_failed = False
_influx_write_failed_logged = False


def _get_influx():
    global _influx_client, _influx_failed
    if _influx_client is not None:
        return _influx_client
    if _influx_failed:
        return None
    url = os.environ.get("INFLUXDB_URL", "")
    if not url:
        return None  # backend not configured — intended disabled path, stay silent
    try:
        from influxdb_client_3 import InfluxDBClient3
        _influx_client = InfluxDBClient3(
            host=url,
            token=os.environ.get("INFLUXDB_TOKEN", ""),
            database=os.environ.get("INFLUXDB_BUCKET", "backrest-mcp"),
        )
    except Exception as exc:
        _influx_failed = True
        log.warning(
            "influx_init_failed",
            error_class=type(exc).__name__,
            detail=(
                "INFLUXDB_URL is set but the client could not be built; metric "
                "writes are disabled for the lifetime of this process. Check the "
                "URL is reachable and the influxdb3-python extra is installed."
            ),
        )
    return _influx_client


_nats_client = None
_nats_failed = False
_nats_publish_failed_logged = False
_nats_error_logged = False

# nats-py's own reconnect machinery is the larger half of the flood — the per-call
# re-entry fixed by the sentinel above is only the other half. Measured against a
# refused port on 2026-08-30 with this repo's own code: ONE default
# nats.connect() blocked the calling tool for 120.2 s and invoked error_cb 61
# times before it ever raised, because _select_next_server() loops with
# max_reconnect_attempts=60 / reconnect_time_wait=2 and reports every failed
# attempt.
#
# Note max_reconnect_attempts=0 is a trap: the discard check in _select_next_server
# is `if self.options["max_reconnect_attempts"] > 0`, so 0 never discards the server
# and retries *forever*. 1 is the fail-fast value (two attempts, then NoServersError).
#
# allow_reconnect=False additionally stops a dropped connection from spawning the
# background retry loop that was never closed.
_NATS_CONNECT_OPTS = {
    "allow_reconnect": False,
    "max_reconnect_attempts": 1,
    "reconnect_time_wait": 0,
    "connect_timeout": 2,
}
# Hard ceiling on how long a broken NATS may delay a tool call, whatever the
# library does internally.
_NATS_CONNECT_DEADLINE = 5.0


async def _nats_error_cb(exc: Exception) -> None:
    """Replace nats-py's default error callback.

    The default is ``_logger.error("nats: encountered error", exc_info=ex)`` on the
    ``nats.aio.client`` logger — at ERROR, so demoting that logger to WARNING (see
    ``_THIRD_PARTY_LOGGERS``) does **not** silence it, and it fires once per
    reconnect attempt. Warn once per process and drop the rest, carrying the
    exception class only: a NATS URL is ``nats://user:password@host``.
    """
    global _nats_error_logged
    if not _nats_error_logged:
        _nats_error_logged = True
        log.warning(
            "nats_transport_error",
            error_class=type(exc).__name__,
            detail="first NATS transport error; further ones are not logged",
        )


async def _get_nats():
    global _nats_client, _nats_failed
    if _nats_client is not None:
        return _nats_client
    if _nats_failed:
        return None
    url = os.environ.get("NATS_URL", "")
    if not url:
        return None  # backend not configured — intended disabled path, stay silent
    try:
        import nats
        # SECURITY[deferred]: no credential support — forge NATS requires per-agent auth.
        # A NATS_URL set without creds now fails *loudly and once* (below) rather than
        # silently. NATS telemetry is not currently enabled for this server.
        # Target: when NATS telemetry is enabled for this server. Audit: 2026-06-04/backrest-mcp-2026-06. Ticket: BKRST-1.
        _nats_client = await asyncio.wait_for(
            nats.connect(url, error_cb=_nats_error_cb, **_NATS_CONNECT_OPTS),
            timeout=_NATS_CONNECT_DEADLINE,
        )
    except Exception as exc:
        _nats_failed = True
        log.warning(
            "nats_init_failed",
            error_class=type(exc).__name__,
            detail=(
                "NATS_URL is set but the connection failed; metric publishes are "
                "disabled for the lifetime of this process. Check the URL and that "
                "a NATS user is provisioned for this service."
            ),
        )
    return _nats_client


async def emit_metric(
    measurement: str,
    tags: dict[str, str],
    fields: dict[str, Any],
) -> None:
    global _influx_write_failed_logged, _nats_publish_failed_logged

    influx = _get_influx()
    if influx:
        try:
            from influxdb_client_3 import Point
            p = Point(measurement)
            for k, v in tags.items():
                p = p.tag(k, v)
            for k, v in fields.items():
                p = p.field(k, v)
            influx.write(record=p)
        except Exception as exc:
            # THE half that matters (vikunja#580). `InfluxDBClient3` constructs
            # lazily and never contacts the host, so `_get_influx()` *succeeds*
            # against an unreachable URL and the init sentinel above never fires —
            # verified live 2026-08-30 against http://127.0.0.1:9/nope, where the
            # client built fine and every write() raised NewConnectionError into
            # this `except`. Without this warning the sentinel is dead code on the
            # only path that runs, and a misconfigured InfluxDB stays exactly as
            # silent as it was for 35 days.
            #
            # Warn once per process, then stay quiet. Unlike an init failure a
            # write failure is often transient (collector restart), so it does not
            # disable the backend — but it must not be silent either.
            if not _influx_write_failed_logged:
                _influx_write_failed_logged = True
                log.warning(
                    "influx_write_failed",
                    measurement=measurement,
                    error_class=type(exc).__name__,
                    detail="first metric write failure; further ones are not logged",
                )

    nats_client = await _get_nats()
    if nats_client:
        try:
            import json
            prefix = os.environ.get("NATS_SUBJECT_PREFIX", "backrest")
            tool = tags.get("tool", "unknown")
            subject = f"{prefix}.tool.{tool}"
            payload = json.dumps({"measurement": measurement, "tags": tags, "fields": fields})
            await nats_client.publish(subject, payload.encode())
        except Exception as exc:
            if not _nats_publish_failed_logged:
                _nats_publish_failed_logged = True
                log.warning(
                    "nats_publish_failed",
                    measurement=measurement,
                    error_class=type(exc).__name__,
                    detail="first metric publish failure; further ones are not logged",
                )
