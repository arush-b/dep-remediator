"""The interface each language must provide (section 8.1 of the design doc).

Only the operations in use are listed. Applying upgrades arrives in M1 and code indexing in M2.
"""

from pathlib import Path
from typing import Protocol

from remediator.models import (
    BuildPlan,
    DependencyGraph,
    Diagnostic,
    PackageId,
    ProjectRoot,
)
from remediator.sandbox import Sandbox

type DepPath = list[PackageId]  # From the project down to one package.


class AdapterError(Exception):
    """A tool's output could not be understood, or a required step failed."""


class UnsupportedProject(AdapterError):
    """The project was recognized, but this version can't handle it."""


class EcosystemAdapter(Protocol):
    @property
    def image(self) -> str:
        """The container image with this language's tools."""
        ...

    @property
    def sandbox_env(self) -> dict[str, str]:
        """Environment variables for commands in the sandbox. Never put credentials here."""
        ...

    def detect(self, repo: Path) -> list[ProjectRoot]:
        """Find the projects in a repository. Reads files only and runs nothing."""
        ...

    def build_commands(self, root: ProjectRoot) -> BuildPlan:
        """Decide which commands install, build and test this project."""
        ...

    async def resolve_graph(self, sandbox: Sandbox, root: ProjectRoot) -> DependencyGraph:
        """Read the installed dependency tree. Runs after install."""
        ...

    def dependency_paths(self, graph: DependencyGraph, pkg: PackageId) -> list[DepPath]:
        """Explain why a package is present: the chains that lead to it from the project."""
        ...

    def parse_diagnostics(self, output: str) -> list[Diagnostic]:
        """Turn compiler output into structured errors."""
        ...
