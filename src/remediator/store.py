"""Where run data lives. M0 uses files; Postgres can implement the same interface later."""

import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from remediator.state import Run

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


class RunStore(Protocol):
    def save_run(self, run: Run) -> None: ...

    def load_run(self, run_id: str) -> Run: ...

    def save_artifact(self, run_id: str, name: str, content: str) -> None: ...

    def load_artifact(self, run_id: str, name: str) -> str: ...


class FileRunStore:
    """Stores each run in its own directory: <base>/<run id>/."""

    def __init__(self, base: Path) -> None:
        self.base = base

    def run_dir(self, run_id: str) -> Path:
        path = self.base / _checked(run_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_run(self, run: Run) -> None:
        _write_atomic(self.run_dir(run.id) / "run.json", run.model_dump_json(indent=2))

    def load_run(self, run_id: str) -> Run:
        return Run.model_validate_json((self.base / _checked(run_id) / "run.json").read_text())

    def save_artifact(self, run_id: str, name: str, content: str) -> None:
        _write_atomic(self.run_dir(run_id) / _checked(name), content)

    def load_artifact(self, run_id: str, name: str) -> str:
        return (self.base / _checked(run_id) / _checked(name)).read_text()


def _checked(name: str) -> str:
    """Reject names that could point outside the store, such as ../../etc/passwd."""
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(f"unsafe name: {name!r}")
    return name


def _write_atomic(path: Path, content: str) -> None:
    """Write to a temporary file, then swap it in, so a crash can't leave a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
