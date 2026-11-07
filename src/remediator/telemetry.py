"""Tracing. Each run writes its spans to a local file, and to an OTLP backend if one is set."""

import json
import os
from collections.abc import Sequence
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

TRACER_NAME = "remediator"


class JsonlSpanExporter(SpanExporter):
    """Appends each finished span to a file as one line of JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self.path.open("a") as f:
            for span in spans:
                f.write(json.dumps(json.loads(span.to_json())) + "\n")
        return SpanExportResult.SUCCESS


def setup_tracing(trace_file: Path) -> TracerProvider:
    """Call once per process, and call shutdown() on the result before exiting."""
    provider = TracerProvider(resource=Resource.create({"service.name": "remediator"}))
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(trace_file)))

    endpoint = os.environ.get("REMEDIATOR_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)
