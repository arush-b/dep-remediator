"""Tests that start real containers and try to break each sandbox protection."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from remediator.models import CommandResult
from remediator.sandbox import DEFAULT_MAX_OUTPUT, DockerSandbox, SandboxError, SandboxLimits

pytestmark = pytest.mark.docker

IMAGE = "node:22-bookworm-slim"
FETCH = (
    "fetch('https://registry.npmjs.org/')"
    ".then(() => process.exit(0), () => process.exit(1))"
)


async def test_runs_commands_and_captures_output_and_exit_code() -> None:
    async with DockerSandbox(IMAGE) as sandbox:
        sandbox_ok = await _in_empty_workspace(sandbox, ["sh", "-c", "echo hello; exit 0"])
        sandbox_bad = await _in_empty_workspace(sandbox, ["sh", "-c", "echo oops >&2; exit 3"])

    assert sandbox_ok.ok
    assert sandbox_ok.output.strip() == "hello"
    assert sandbox_bad.exit_code == 3
    assert not sandbox_bad.ok
    assert "oops" in sandbox_bad.output


async def test_network_works_until_it_is_cut() -> None:
    async with DockerSandbox(IMAGE, network=True) as sandbox:
        before = await _in_empty_workspace(sandbox, ["node", "-e", FETCH])
        await sandbox.disable_network()
        after = await _in_empty_workspace(sandbox, ["node", "-e", FETCH])

    assert before.ok, "the check must be able to succeed, or it proves nothing"
    assert after.exit_code == 1


async def test_no_network_unless_asked_for() -> None:
    async with DockerSandbox(IMAGE) as sandbox:
        result = await _in_empty_workspace(sandbox, ["node", "-e", FETCH])

    assert result.exit_code == 1


async def test_commands_are_not_root_and_have_no_privileges() -> None:
    async with DockerSandbox(IMAGE) as sandbox:
        uid = await _in_empty_workspace(sandbox, ["id", "-u"])
        caps = await _in_empty_workspace(sandbox, ["grep", "CapEff", "/proc/self/status"])

    assert uid.output.strip() == "1000"
    assert caps.output.split()[1] == "0000000000000000"


async def test_secrets_and_history_are_not_copied_in(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export {};")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / ".env.production").write_text("SECRET=2")
    (tmp_path / ".env.example").write_text("SECRET=")
    (tmp_path / ".npmrc").write_text("//registry.npmjs.org/:_authToken=abc")
    (tmp_path / "src" / ".env").write_text("SECRET=3")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "a.js").write_text("")

    async with DockerSandbox(IMAGE) as sandbox:
        await sandbox.copy_in(tmp_path)
        listing = await sandbox.exec(["find", ".", "-mindepth", "1", "-printf", "%u %p\n"])
        write = await sandbox.exec(["touch", "new-file"])

    lines = listing.output.splitlines()
    files = {line.split(" ", 1)[1] for line in lines}
    assert files == {"./package.json", "./src", "./src/index.ts", "./.env.example"}
    assert all(line.startswith("node ") for line in lines)
    assert write.ok, "the non-root user must be able to write in the workspace"


async def test_changes_in_the_sandbox_do_not_reach_the_host(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("original")

    async with DockerSandbox(IMAGE) as sandbox:
        await sandbox.copy_in(tmp_path)
        await sandbox.exec(["sh", "-c", "echo hacked > file.txt; touch planted.txt"])

    assert (tmp_path / "file.txt").read_text() == "original"
    assert not (tmp_path / "planted.txt").exists()


async def test_hung_commands_are_stopped() -> None:
    async with DockerSandbox(IMAGE) as sandbox:
        result = await _in_empty_workspace(sandbox, ["sleep", "60"], timeout_s=1)

    assert result.timed_out
    assert not result.ok
    assert result.duration_s < 20


async def test_output_is_capped_and_keeps_the_end() -> None:
    script = "yes | head -c 200000; echo THE-END"
    async with DockerSandbox(IMAGE) as sandbox:
        result = await _in_empty_workspace(sandbox, ["sh", "-c", script], max_output=1024)

    assert result.truncated
    assert len(result.output) == 1024
    assert result.output.strip().endswith("THE-END")


async def test_memory_limit_kills_the_command() -> None:
    hog = "const a = []; while (true) a.push(Buffer.alloc(1e7, 1));"
    limits = SandboxLimits(memory_mb=128)
    async with DockerSandbox(IMAGE, limits=limits) as sandbox:
        result = await _in_empty_workspace(sandbox, ["node", "-e", hog], timeout_s=60)

    assert result.exit_code == 137
    assert not result.timed_out


@pytest.mark.parametrize("cwd", ["../etc", "/etc", "a/../../etc"])
async def test_working_directory_cannot_leave_the_workspace(cwd: str) -> None:
    async with DockerSandbox(IMAGE) as sandbox:
        with pytest.raises(SandboxError):
            await sandbox.exec(["pwd"], cwd=cwd)


async def test_container_is_removed_after_an_error() -> None:
    container = ""
    with pytest.raises(RuntimeError):
        async with DockerSandbox(IMAGE) as sandbox:
            container = sandbox.id
            raise RuntimeError("something went wrong mid-run")

    assert container
    remaining = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", f"id={container}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert remaining.stdout.strip() == ""


async def test_exec_after_exit_is_refused() -> None:
    async with DockerSandbox(IMAGE) as sandbox:
        pass

    with pytest.raises(SandboxError):
        await sandbox.exec(["pwd"])


async def _in_empty_workspace(
    sandbox: DockerSandbox,
    argv: list[str],
    *,
    timeout_s: int = 900,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> CommandResult:
    """Run a command in /workspace, creating it first because nothing has been copied in."""
    with tempfile.TemporaryDirectory() as empty:
        await sandbox.copy_in(Path(empty))
    return await sandbox.exec(argv, timeout_s=timeout_s, max_output=max_output)
