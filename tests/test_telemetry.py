"""M0 requires runs to be fully traced, so check what actually reaches the trace file."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from remediator.telemetry import JsonlSpanExporter


def read_spans(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_spans_reach_the_file_with_names_attributes_and_parents(tmp_path: Path) -> None:
    trace_file = tmp_path / "trace.jsonl"
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(trace_file)))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("stage.baseline") as stage:
        stage.set_attribute("sandbox.id", "abc123")
        with tracer.start_as_current_span("tool.sandbox_exec") as tool:
            tool.set_attribute("exit_code", 0)
    provider.shutdown()

    child, parent = read_spans(trace_file)  # A span is written when it finishes.
    assert parent["name"] == "stage.baseline"
    assert parent["attributes"]["sandbox.id"] == "abc123"
    assert parent["parent_id"] is None
    assert child["name"] == "tool.sandbox_exec"
    assert child["attributes"]["exit_code"] == 0
    assert child["parent_id"] == parent["context"]["span_id"]
    assert child["context"]["trace_id"] == parent["context"]["trace_id"]


def test_spans_from_separate_exports_are_all_kept(tmp_path: Path) -> None:
    trace_file = tmp_path / "trace.jsonl"
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(trace_file)))
    tracer = provider.get_tracer("test")

    for name in ("first", "second"):
        with tracer.start_as_current_span(name):
            pass
    provider.shutdown()

    assert [span["name"] for span in read_spans(trace_file)] == ["first", "second"]


def test_setup_tracing_writes_spans_from_the_shared_tracer(tmp_path: Path) -> None:
    """Runs in a new process because tracing can only be set up once per process."""
    trace_file = tmp_path / "trace.jsonl"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from remediator.telemetry import setup_tracing, tracer\n"
        "provider = setup_tracing(Path(sys.argv[1]))\n"
        "with tracer().start_as_current_span('remediation.run'):\n"
        "    pass\n"
        "provider.shutdown()\n"
    )

    subprocess.run([sys.executable, "-c", script, str(trace_file)], check=True)

    span = read_spans(trace_file)[0]
    assert span["name"] == "remediation.run"
    assert span["resource"]["attributes"]["service.name"] == "remediator"
