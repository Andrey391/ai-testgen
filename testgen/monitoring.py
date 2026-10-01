"""Prometheus metrics (stage 5.5): GET /metrics of the studio, and of a worker on
TESTGEN_METRICS_PORT. With TESTGEN_METRICS_TOKEN set, /metrics wants "Authorization: Bearer <token>".

    testgen_http_requests_total{method,route,status}   requests to the API (routes as templates, no ids)
    testgen_http_request_duration_seconds{route}       their duration
    testgen_runs_total{status,trigger}                 finished runs of saved tests (this process)
    testgen_llm_requests_total{stage,provider,model}   requests to language models (this process)
    testgen_studio_sessions                            live Studio sessions of this instance
    testgen_queue_items{status}                        the work queue (shared database)
    testgen_queue_oldest_seconds                       how long the oldest queued item waits
    testgen_workers / testgen_worker_slots{state}      workers alive, their busy and free slots
"""
from __future__ import annotations

import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, start_http_server
from prometheus_client.core import GaugeMetricFamily

REGISTRY = CollectorRegistry()
REQUESTS = Counter("testgen_http_requests_total", "API requests", ["method", "route", "status"], registry=REGISTRY)
LATENCY = Histogram("testgen_http_request_duration_seconds", "API request duration", ["route"], registry=REGISTRY,
                    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
RUNS = Counter("testgen_runs_total", "Finished runs of saved tests", ["status", "trigger"], registry=REGISTRY)
LLM = Counter("testgen_llm_requests_total", "Requests to language models", ["stage", "provider", "model"],
              registry=REGISTRY)
SESSIONS = Gauge("testgen_studio_sessions", "Live Studio sessions of this instance", registry=REGISTRY)
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class _Queue:
    """Read from the shared database at scrape time."""

    def collect(self):
        from . import workqueue
        if not workqueue.enabled():
            return
        try:
            s = workqueue.stats()
        except Exception:
            return
        items = GaugeMetricFamily("testgen_queue_items", "Work queue items", labels=["status"])
        for status in ("queued", "running", "done", "failed"):
            items.add_metric([status], s[status])
        yield items
        yield GaugeMetricFamily("testgen_queue_oldest_seconds", "Wait of the oldest queued item",
                                value=s["oldest_queued_seconds"])
        yield GaugeMetricFamily("testgen_workers", "Workers alive", value=s["workers"])
        slots = GaugeMetricFamily("testgen_worker_slots", "Slots of the workers", labels=["state"])
        slots.add_metric(["busy"], s["busy"])
        slots.add_metric(["free"], max(0, s["capacity"] - s["busy"]))
        yield slots


REGISTRY.register(_Queue())


def observe(method: str, route: str, status: int, started: float) -> None:
    route = route or "other"
    REQUESTS.labels(method, route, str(status)).inc()
    LATENCY.labels(route).observe(time.time() - started)


def render() -> bytes:
    return generate_latest(REGISTRY)


def serve_worker(port: int) -> None:
    start_http_server(port, registry=REGISTRY)
