"""C-035 regression test: readiness and guard-releasing shutdown.

The positive leg proves readiness only turns true once the SDK is initialized and
the query plane is usable, and that an in-flight query really holds the native
guard while it runs. The boundary leg proves a drain that runs out of time
cancels the remaining work and still releases every guard, so a restart cannot
inherit a stranded lock. The negative legs prove the refusals are real: a query
admitted after the drain begins is refused with NOT_READY, an uninitialized SDK
is never reported ready, a raising probe is reported as unavailable rather than
swallowed, and out-of-range drain budgets are rejected instead of silently
clamped.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from axiom_mcp import http, lifecycle, sdk_compat
from axiom_mcp.errors import AxiomError
from axiom_mcp.guard.engine import SolutionGuard
from tests.test_tools_support import guard_dir

BASE_URL = http.DEFAULT_BASE_URL
ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*"]
ALLOWED_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*"]


def _wait_until(predicate, *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


async def _readyz(readiness) -> tuple[int, dict]:
    server = sdk_compat.build_server(
        "c-035-test", allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS
    )
    gateway = http.build_gateway(server, readiness=readiness)
    async with sdk_compat.open_lifespan(gateway.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url=BASE_URL, timeout=30.0
        ) as client:
            response = await client.get(http.READY_PATH)
    return response.status_code, response.json()


# -- readiness -------------------------------------------------------------


def test_transport_default_is_not_ready() -> None:
    report = lifecycle.default_readiness()
    assert report.ready is False
    assert report.as_dict()["error"] == lifecycle.NOT_READY
    assert report.query.name == lifecycle.QUERY_COMPONENT
    assert report.control.name == lifecycle.CONTROL_COMPONENT


def test_readiness_requires_an_initialized_sdk_and_a_usable_query_plane() -> None:
    lc = lifecycle.Lifecycle()
    before = lc.readiness()
    assert before.ready is False
    assert before.query.available is False
    assert before.query.detail == lifecycle.SDK_NOT_INITIALIZED

    lc.mark_sdk_initialized()
    after_sdk = lc.readiness()
    assert after_sdk.query.available is False
    assert after_sdk.query.detail == lifecycle.QUERY_NOT_WIRED

    lc.mark_query_available()
    lc.mark_control_available()
    ready = lc.readiness()
    assert ready.ready is True
    assert ready.as_dict()["error"] is None


def test_control_plane_stays_distinct_when_the_query_plane_is_up() -> None:
    lc = lifecycle.Lifecycle()
    lc.mark_sdk_initialized()
    lc.mark_query_available()
    lc.mark_control_available(False, "daemon not running")
    report = lc.readiness()
    assert report.query.available is True
    assert report.control.available is False
    assert report.control.detail == "daemon not running"
    assert report.ready is False


def test_a_raising_query_probe_is_reported_as_unavailable_not_swallowed() -> None:
    def probe():
        raise RuntimeError("daemon down")

    lc = lifecycle.Lifecycle(query_probe=probe)
    lc.mark_sdk_initialized()
    report = lc.readiness()
    assert report.query.available is False
    assert report.query.detail == "query_probe_failed:RuntimeError"
    assert report.ready is False


def test_a_wired_probe_overrides_a_stale_availability_mark() -> None:
    lc = lifecycle.Lifecycle(query_probe=lambda: False)
    lc.mark_sdk_initialized()
    lc.mark_query_available(True)
    lc.mark_control_available(True)
    report = lc.readiness()
    assert report.query.available is False
    assert report.ready is False


def test_a_wired_probe_may_return_a_full_component_report() -> None:
    lc = lifecycle.Lifecycle(
        query_probe=lambda: lifecycle.ComponentReadiness(lifecycle.QUERY_COMPONENT, True, None)
    )
    lc.mark_sdk_initialized()
    lc.mark_control_available()
    assert lc.readiness().ready is True


def test_the_transport_reuses_the_lifecycle_readiness_types() -> None:
    assert http.ComponentReadiness is lifecycle.ComponentReadiness
    assert http.ReadinessReport is lifecycle.ReadinessReport
    assert http.NOT_READY == lifecycle.NOT_READY
    assert http.default_readiness().as_dict() == lifecycle.default_readiness().as_dict()


def test_readyz_reports_503_unready_then_200_once_the_planes_are_up() -> None:
    lc = lifecycle.Lifecycle()

    status, body = asyncio.run(_readyz(lc.readiness))
    assert status == 503
    assert body["error"] == lifecycle.NOT_READY
    assert body["query"] == {"available": False, "detail": "sdk_not_initialized"}

    lc.mark_sdk_initialized()
    lc.mark_query_available()
    lc.mark_control_available()

    status_ready, body_ready = asyncio.run(_readyz(lc.readiness))
    assert status_ready == 200
    assert body_ready["ready"] is True
    assert body_ready["error"] is None
    assert body_ready["query"] == {"available": True}
    assert body_ready["control"] == {"available": True}


# -- admission and shutdown ------------------------------------------------


def test_shutdown_lets_an_in_flight_query_finish_and_leaves_no_guard_held(tmp_path) -> None:
    guard = SolutionGuard(guard_dir(tmp_path), timeout_ms=2000)
    lc = lifecycle.Lifecycle(guard=guard, drain_timeout_s=5.0)
    lc.mark_sdk_initialized()
    lc.mark_query_available()

    started = threading.Event()
    release = threading.Event()
    observed: dict = {}

    def run_query() -> None:
        with lc.query():
            observed["held"] = guard.held_names
            started.set()
            release.wait(timeout=5.0)

    worker = threading.Thread(target=run_query)
    worker.start()
    try:
        assert started.wait(timeout=5.0), "the query never started"
        # The leg is only meaningful if the query really held the guard.
        assert guard.held_names != ()
        assert observed["held"] != ()
        assert lc.in_flight == 1

        release.set()
        outcome = lc.shutdown(timeout_s=5.0)
    finally:
        release.set()
        worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert outcome.drained is True
    assert outcome.cancelled == 0
    assert outcome.guards_held == ()
    assert guard.held_names == ()
    assert lc.in_flight == 0


def test_a_drain_that_runs_out_of_time_cancels_and_still_releases_the_guard(tmp_path) -> None:
    guard = SolutionGuard(guard_dir(tmp_path), timeout_ms=2000)
    lc = lifecycle.Lifecycle(guard=guard, drain_timeout_s=5.0)
    lc.mark_sdk_initialized()
    lc.mark_query_available()

    started = threading.Event()
    stop = threading.Event()

    def run_query() -> None:
        with lc.query():
            started.set()
            stop.wait(timeout=5.0)

    worker = threading.Thread(target=run_query)
    worker.start()
    try:
        assert started.wait(timeout=5.0)
        assert guard.held_names != ()

        outcome = lc.drain(timeout_s=0.2)
        assert outcome.drained is False
        assert outcome.remaining == 1
        assert outcome.cancelled == 1
        # The guard is released even though the query had not finished.
        assert outcome.guards_held == ()
        assert guard.held_names == ()
    finally:
        stop.set()
        worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert lc.in_flight == 0


def test_a_query_admitted_after_the_drain_begins_is_refused() -> None:
    lc = lifecycle.Lifecycle()
    lc.start_drain()
    assert lc.draining is True
    with pytest.raises(AxiomError) as excinfo:
        with lc.query():
            pass
    assert excinfo.value.code == "NOT_READY"
    assert lc.in_flight == 0


def test_shutdown_twice_is_idempotent_and_does_not_double_count(tmp_path) -> None:
    guard = SolutionGuard(guard_dir(tmp_path), timeout_ms=2000)
    lc = lifecycle.Lifecycle(guard=guard)
    first = lc.shutdown(timeout_s=1.0)
    second = lc.shutdown(timeout_s=1.0)
    assert first is second
    assert second.drained is True
    assert second.cancelled == 0
    assert second.guards_held == ()


def test_a_query_without_a_guard_still_counts_in_flight() -> None:
    lc = lifecycle.Lifecycle()
    with lc.query():
        assert lc.in_flight == 1
        assert lc.draining is False
    assert lc.in_flight == 0


@pytest.mark.parametrize("timeout_s", [0, 0.0, -1.0, 60.5, 61, True, "5"])
def test_a_drain_budget_outside_the_declared_range_is_rejected(timeout_s) -> None:
    with pytest.raises(AxiomError) as excinfo:
        lifecycle.Lifecycle(drain_timeout_s=timeout_s)
    assert excinfo.value.code == "LIMIT_EXCEEDED"


def test_a_per_call_drain_budget_outside_the_declared_range_is_rejected() -> None:
    lc = lifecycle.Lifecycle()
    for bad in (0, 0.0, 60.5, True, "5"):
        with pytest.raises(AxiomError) as excinfo:
            lc.drain(timeout_s=bad)
        assert excinfo.value.code == "LIMIT_EXCEEDED"
