"""Shared data types passed between components."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PackageManager(StrEnum):
    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"


class ProjectRoot(BaseModel):
    """One installable project inside a repository."""

    path: str  # Relative to the repository root, using forward slashes. "." is the root itself.
    package_manager: PackageManager
    has_lockfile: bool = False
    has_typescript: bool = False
    scripts: dict[str, str] = {}


class PackageId(BaseModel):
    """One package at one exact version."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: str

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"


class DependencyGraph(BaseModel):
    """Which package depends on which. The root is the project itself."""

    root: PackageId
    packages: list[PackageId] = []
    edges: list[tuple[PackageId, PackageId]] = []  # (dependent, dependency)


class BuildStep(BaseModel):
    """One command to run in the sandbox."""

    name: str
    argv: list[str]
    timeout_s: int = 900
    parse_diagnostics: bool = False


class BuildPlan(BaseModel):
    """Install runs with network access; every check runs with the network cut."""

    install: BuildStep
    checks: list[BuildStep] = []


class CommandResult(BaseModel):
    argv: list[str]
    exit_code: int
    output: str
    duration_s: float
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Diagnostic(BaseModel):
    """One compiler error or warning, in a form that can be routed back to a file's owner."""

    file: str
    line: int
    column: int
    severity: str
    code: str
    message: str


class StepResult(BaseModel):
    name: str
    result: CommandResult
    diagnostics: list[Diagnostic] = []


class BaselineReport(BaseModel):
    """Build and test results for the unchanged code."""

    run_id: str
    project: ProjectRoot
    passed: bool
    steps: list[StepResult] = []
    package_count: int | None = None
