"""Runs untrusted build and test commands inside a throwaway Docker container.

Every build is treated as hostile (section 11 of the design doc). This module knows nothing about
any particular language: it runs whatever commands it is given.

Known gaps against the design doc, to be closed in later milestones:
- While network access is on, egress is not limited to package registries.
- Plain Docker only. There is no gVisor or Firecracker backend yet.
- No disk limit inside the container.
Until those are closed, only run repositories you trust.
"""

import asyncio
import os
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import IO, Protocol, Self

from remediator.models import CommandResult
from remediator.telemetry import tracer

WORKSPACE = "workspace"  # Lives at /workspace in the container.
DEFAULT_MAX_OUTPUT = 1024 * 1024  # 1 MiB of command output is kept; older output is dropped.
_READ_CHUNK = 64 * 1024
_DOCKER_TIMEOUT_S = 600  # Bound for Docker's own operations, including pulling an image.
_CLIENT_GRACE_S = 30  # Extra time for the in-container timeout to fire before we give up on it.

_EXCLUDED_NAMES = {".git", "node_modules", ".remediator", ".env", ".npmrc"}
_ALLOWED_ENV_FILES = {".env.example", ".env.sample", ".env.template"}


class SandboxError(Exception):
    """A sandbox could not be set up or controlled. The run must not continue."""


@dataclass(frozen=True)
class SandboxLimits:
    cpus: float = 2.0
    memory_mb: int = 3072
    pids: int = 512

    @classmethod
    def from_env(cls) -> Self:
        """Read limits from the environment, so a bigger machine can raise them."""
        return cls(
            cpus=float(os.environ.get("REMEDIATOR_SANDBOX_CPUS", cls.cpus)),
            memory_mb=int(os.environ.get("REMEDIATOR_SANDBOX_MEMORY_MB", cls.memory_mb)),
            pids=int(os.environ.get("REMEDIATOR_SANDBOX_PIDS", cls.pids)),
        )


class Sandbox(Protocol):
    """What the rest of the system may do with a sandbox."""

    @property
    def id(self) -> str: ...

    async def copy_in(self, repo: Path) -> None: ...

    async def disable_network(self) -> None: ...

    async def exec(
        self,
        argv: list[str],
        *,
        cwd: str = ".",
        timeout_s: int = 900,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> CommandResult: ...


class DockerSandbox:
    """Use with `async with`, so the container is removed even if something fails."""

    def __init__(
        self,
        image: str,
        *,
        network: bool = False,
        limits: SandboxLimits | None = None,
        env: dict[str, str] | None = None,
        user: str = "1000:1000",
    ) -> None:
        self._image = image
        self._network = network
        self._limits = limits or SandboxLimits.from_env()
        self._env = env or {}
        self._user = user
        self._id: str | None = None

    @property
    def id(self) -> str:
        if self._id is None:
            raise SandboxError("sandbox is not running")
        return self._id

    async def __aenter__(self) -> Self:
        limits = self._limits
        args = [
            "run",
            "--detach",
            "--init",
            "--user",
            self._user,
            "--network",
            "bridge" if self._network else "none",
            "--memory",
            f"{limits.memory_mb}m",
            "--memory-swap",
            f"{limits.memory_mb}m",  # Same as --memory, which means no swap.
            "--cpus",
            str(limits.cpus),
            "--pids-limit",
            str(limits.pids),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--label",
            "remediator.sandbox=true",
        ]
        for key, value in self._env.items():
            args += ["--env", f"{key}={value}"]
        args += [self._image, "sleep", "infinity"]

        code, stdout, stderr = await _docker(*args)
        if code != 0:
            raise SandboxError(f"could not start container: {stderr.strip()}")
        self._id = stdout.strip()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._id is None:
            return
        container, self._id = self._id, None
        code, _, stderr = await _docker("rm", "--force", container)
        if code != 0:
            raise SandboxError(f"could not remove container {container}: {stderr.strip()}")

    async def copy_in(self, repo: Path) -> None:
        """Copy the repository to /workspace, keeping the host's files out of reach."""
        with tempfile.TemporaryFile() as archive:
            await asyncio.to_thread(_write_archive, repo, archive)
            archive.seek(0)
            code, _, stderr = await _docker("cp", "--archive", "-", f"{self.id}:/", stdin=archive)
        if code != 0:
            raise SandboxError(f"could not copy repository: {stderr.strip()}")

    async def disable_network(self) -> None:
        """Cut network access. Failure raises, so untrusted code can't run with network on."""
        if not self._network:
            return
        code, _, stderr = await _docker("network", "disconnect", "bridge", self.id)
        if code != 0:
            raise SandboxError(f"could not cut network access: {stderr.strip()}")
        self._network = False

    async def exec(
        self,
        argv: list[str],
        *,
        cwd: str = ".",
        timeout_s: int = 900,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> CommandResult:
        """Run one command in the container and return its result. Never runs on the host."""
        command = [
            "docker",
            "exec",
            "--workdir",
            _workdir(cwd),
            self.id,
            # The timeout runs inside the container, so it stops the command itself.
            "timeout",
            "--kill-after=5",
            str(timeout_s),
            *argv,
        ]
        with tracer().start_as_current_span("tool.sandbox_exec") as span:
            span.set_attribute("sandbox.id", self.id)
            span.set_attribute("command", " ".join(argv))

            start = time.monotonic()
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            buffer = _TailBuffer(max_output)
            try:
                async with asyncio.timeout(timeout_s + _CLIENT_GRACE_S):
                    await buffer.read_from(process)
                    exit_code = await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
                exit_code = -1
            duration = time.monotonic() - start

            # `timeout` exits with 124, or 137 if it had to force-kill.
            timed_out = exit_code == -1 or (exit_code in (124, 137) and duration >= timeout_s)

            span.set_attribute("exit_code", exit_code)
            span.set_attribute("duration_s", duration)
            span.set_attribute("timed_out", timed_out)
            return CommandResult(
                argv=argv,
                exit_code=exit_code,
                output=buffer.text(),
                duration_s=duration,
                timed_out=timed_out,
                truncated=buffer.truncated,
            )


class _TailBuffer:
    """Keeps only the end of a command's output, so a noisy build can't exhaust memory."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self.truncated = False

    async def read_from(self, process: asyncio.subprocess.Process) -> None:
        stream = process.stdout
        if stream is None:
            raise SandboxError("command output is not available")
        while chunk := await stream.read(_READ_CHUNK):
            self._data += chunk
            if len(self._data) > self._limit:
                del self._data[: len(self._data) - self._limit]
                self.truncated = True

    def text(self) -> str:
        return self._data.decode("utf-8", errors="replace")


def _workdir(cwd: str) -> str:
    """Turn a path relative to the workspace into an absolute one, refusing to leave it."""
    path = PurePosixPath(cwd)
    if path.is_absolute() or ".." in path.parts:
        raise SandboxError(f"working directory must stay inside the workspace: {cwd!r}")
    return str(PurePosixPath("/") / WORKSPACE / path)


def _is_excluded(name: str) -> bool:
    if name in _ALLOWED_ENV_FILES:
        return False
    return name in _EXCLUDED_NAMES or name.startswith(".env.")


def _write_archive(repo: Path, archive: IO[bytes]) -> None:
    """Write the repository as a tar archive, without secrets, history or installed packages."""

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if _is_excluded(PurePosixPath(info.name).name):
            return None
        info.uid = info.gid = 1000  # The container's non-root user must own the files.
        info.uname = info.gname = ""
        return info

    with tarfile.open(fileobj=archive, mode="w") as tar:
        tar.add(repo, arcname=WORKSPACE, filter=keep)


async def _docker(*args: str, stdin: IO[bytes] | None = None) -> tuple[int, str, str]:
    """Run a Docker command on the host. Returns (exit code, stdout, stderr)."""
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=stdin if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(_DOCKER_TIMEOUT_S):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        return -1, "", f"docker {args[0]} timed out after {_DOCKER_TIMEOUT_S}s"
    code = process.returncode if process.returncode is not None else -1
    return code, stdout.decode(errors="replace"), stderr.decode(errors="replace")
