"""Run data must survive crashes, and untrusted names must not escape the store."""

import os
from pathlib import Path

import pytest

from remediator.state import Run, RunState
from remediator.store import FileRunStore, RunStore, new_run_id

UNSAFE_NAMES = ["../../etc/passwd", "..", "a/b", "/etc/passwd", ".hidden", "", "abc\n", "a b"]


def make_run(run_id: str = "run-1") -> Run:
    return Run(id=run_id, repo="acme/app", target="GHSA-xxxx")


def test_run_survives_save_and_load(tmp_path: Path) -> None:
    store: RunStore = FileRunStore(tmp_path)
    run = make_run()
    run.transition(RunState.TRIAGED, reason="deduplicated")

    store.save_run(run)

    assert store.load_run("run-1") == run


def test_saving_again_overwrites(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = make_run()
    store.save_run(run)

    run.transition(RunState.TRIAGED)
    store.save_run(run)

    assert store.load_run("run-1").state is RunState.TRIAGED


def test_artifact_survives_save_and_load(tmp_path: Path) -> None:
    store: RunStore = FileRunStore(tmp_path)

    store.save_artifact("run-1", "baseline.json", '{"passed": true}')

    assert store.load_artifact("run-1", "baseline.json") == '{"passed": true}'


def test_each_run_gets_its_own_folder(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)

    store.save_run(make_run("run-1"))
    store.save_artifact("run-2", "log.txt", "hello")

    assert (tmp_path / "run-1" / "run.json").exists()
    assert (tmp_path / "run-2" / "log.txt").exists()


def test_loading_something_missing_raises(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)

    with pytest.raises(FileNotFoundError):
        store.load_run("no-such-run")
    with pytest.raises(FileNotFoundError):
        store.load_artifact("no-such-run", "log.txt")


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_run_ids_are_rejected(tmp_path: Path, name: str) -> None:
    store = FileRunStore(tmp_path / "runs")

    with pytest.raises(ValueError, match="unsafe name"):
        store.save_run(make_run(name))
    with pytest.raises(ValueError, match="unsafe name"):
        store.load_run(name)
    with pytest.raises(ValueError, match="unsafe name"):
        store.save_artifact(name, "log.txt", "x")
    with pytest.raises(ValueError, match="unsafe name"):
        store.load_artifact(name, "log.txt")


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_artifact_names_are_rejected(tmp_path: Path, name: str) -> None:
    store = FileRunStore(tmp_path / "runs")

    with pytest.raises(ValueError, match="unsafe name"):
        store.save_artifact("run-1", name, "x")
    with pytest.raises(ValueError, match="unsafe name"):
        store.load_artifact("run-1", name)


def test_failed_write_keeps_the_old_file_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    store.save_artifact("run-1", "report.json", "old")

    def crash(*_args: object) -> None:
        raise OSError("disk failed")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="disk failed"):
        store.save_artifact("run-1", "report.json", "new")
    monkeypatch.undo()

    assert store.load_artifact("run-1", "report.json") == "old"
    assert [path.name for path in (tmp_path / "run-1").iterdir()] == ["report.json"]


def test_new_run_ids_are_unique_and_safe_to_store(tmp_path: Path) -> None:
    ids = {new_run_id() for _ in range(100)}

    assert len(ids) == 100
    for run_id in ids:
        FileRunStore(tmp_path).save_run(make_run(run_id))
