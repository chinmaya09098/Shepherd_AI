"""
Application Insights telemetry helpers.

Wraps OpenCensus Azure Monitor exporter for:
  - Custom event tracking (pipeline_completed, email_processed, etc.)
  - Exception / failure tracking
  - Custom metric recording (processing_duration_ms, shipments_extracted)

Usage:
    from src.utils.telemetry import track_event, track_exception, track_metric

    track_event("pipeline_completed", {
        "decision": "auto_approve",
        "email_type": "shipment_tender",
        "message_id": "...",
    })

Telemetry is a no-op when APPINSIGHTS_CONNECTION_STRING is not set,
so local development works without any configuration.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("shepherd.telemetry")

_tracer = None
_stats_recorder = None
_initialized = False


def _init():
    global _tracer, _initialized
    if _initialized:
        return

    conn_str = os.getenv("APPINSIGHTS_CONNECTION_STRING")
    if not conn_str:
        _initialized = True
        return

    try:
        from opencensus.ext.azure import metrics_exporter
        from opencensus.ext.azure.trace_exporter import AzureExporter
        from opencensus.trace.samplers import AlwaysOnSampler
        from opencensus.trace.tracer import Tracer

        _tracer = Tracer(
            exporter=AzureExporter(connection_string=conn_str),
            sampler=AlwaysOnSampler(),
        )
        logger.info("Application Insights telemetry initialised")
    except ImportError:
        logger.debug("opencensus-ext-azure not installed — telemetry disabled")
    except Exception as exc:
        logger.warning("Failed to initialise telemetry: %s", exc)

    _initialized = True


def track_event(name: str, properties: Optional[Dict[str, Any]] = None) -> None:
    """
    Track a named custom event in Application Insights.

    Args:
        name:       Event name (e.g. 'email_processed', 'pipeline_completed')
        properties: Key-value pairs to attach to the event
    """
    _init()
    if _tracer is None:
        return
    try:
        with _tracer.span(name=name) as span:
            span.add_attribute("custom_event", name)
            for k, v in (properties or {}).items():
                span.add_attribute(str(k), str(v))
    except Exception as exc:
        logger.debug("track_event failed (%s): %s", name, exc)


def track_exception(exc: Exception, properties: Optional[Dict[str, Any]] = None) -> None:
    """
    Track an exception in Application Insights.

    Args:
        exc:        The exception to track
        properties: Additional context properties
    """
    _init()
    if _tracer is None:
        return
    try:
        with _tracer.span(name="exception") as span:
            span.add_attribute("exception_type", type(exc).__name__)
            span.add_attribute("exception_message", str(exc))
            for k, v in (properties or {}).items():
                span.add_attribute(str(k), str(v))
    except Exception as te:
        logger.debug("track_exception failed: %s", te)


def track_metric(name: str, value: float, properties: Optional[Dict[str, Any]] = None) -> None:
    """
    Record a numeric metric in Application Insights.

    Args:
        name:  Metric name (e.g. 'processing_duration_ms', 'shipments_extracted')
        value: Numeric value
        properties: Dimensions
    """
    _init()
    if _tracer is None:
        return
    try:
        with _tracer.span(name=f"metric_{name}") as span:
            span.add_attribute("metric_name", name)
            span.add_attribute("metric_value", str(value))
            for k, v in (properties or {}).items():
                span.add_attribute(str(k), str(v))
    except Exception as exc:
        logger.debug("track_metric failed (%s): %s", name, exc)


def track_pipeline_result(result) -> None:
    """
    Convenience function to track a complete PipelineResult.

    Args:
        result: PipelineResult from Orchestrator.process()
    """
    if result is None:
        return

    duration_ms = 0.0
    if result.processing_start and result.processing_end:
        duration_ms = (
            result.processing_end - result.processing_start
        ).total_seconds() * 1000

    properties = {
        "message_id": result.message_id or "",
        "email_type": result.email_type or "",
        "decision": result.decision.value if result.decision else "",
        "response_sent": str(result.response_sent),
        "missing_fields": ",".join(result.missing_fields or []),
        "correlation_id": result.correlation_id or "",
        "error": result.error or "",
    }

    track_event("pipeline_completed", properties)
    track_metric("processing_duration_ms", duration_ms, {"email_type": result.email_type or ""})

    if result.shipments:
        track_metric(
            "shipments_extracted",
            len(result.shipments),
            {"email_type": result.email_type or ""},
        )
