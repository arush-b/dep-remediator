"""TypeScript support, using npm or pnpm."""

import json
import logging
import os
import re
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import Any, cast

from remediator.adapters.base import AdapterError, DepPath, UnsupportedProject
from remediator.models import (
    BuildPlan,
    BuildStep,
    DependencyGraph,
    Diagnostic,
    PackageId,
    PackageManager,
    ProjectRoot,
)
from remediator.sandbox import Sandbox

logger = logging.getLogger(__name__)

IMAGE = "node:22-bookworm-slim"
_SKIPPED_DIRS = {".git", ".remediator", "node_modules", "dist", "build", "vendor"}
_DEPENDENCY_KEYS = ("dependencies", "devDependencies", "optionalDependencies")
_MAX_GRAPH_OUTPUT = 64 * 1024 * 1024  # Dependency trees are large, and JSON can't be cut short.
_MAX_PATHS = 20
_TSC_ERROR = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<column>\d+)\): "
    r"(?P<severity>error|warning) (?P<code>TS\d+): (?P<message>.*)$"
)


class TypeScriptAdapter:
    @property
    def image(self) -> str:
        return IMAGE

    @property
    def sandbox_env(self) -> dict[str, str]:
        return {
            "CI": "true",  # Stops test runners from waiting in watch mode.
            "HOME": "/home/node",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        }

    def detect(self, repo: Path) -> list[ProjectRoot]:
        roots: list[ProjectRoot] = []
        owners: list[PurePosixPath] = []  # Roots that own every package.json beneath them.

        for current, dirs, files in os.walk(repo):
            dirs[:] = sorted(d for d in dirs if d not in _SKIPPED_DIRS)
            if "package.json" not in files:
                continue
            folder = Path(current)
            relative = PurePosixPath(folder.relative_to(repo).as_posix())
            if any(owner in relative.parents for owner in owners):
                continue

            manifest = _read_package_json(folder / "package.json")
            if manifest is None:
                continue
            root = _project_root(relative, folder, set(files), manifest)
            roots.append(root)
            if root.has_lockfile or "workspaces" in manifest or "pnpm-workspace.yaml" in files:
                owners.append(relative)
        return roots

    def build_commands(self, root: ProjectRoot) -> BuildPlan:
        if root.package_manager is PackageManager.YARN:
            raise UnsupportedProject(f"{root.path}: Yarn projects are not supported yet")

        checks: list[BuildStep] = []
        if root.has_typescript:
            tsc = ["tsc", "--noEmit", "--pretty", "false"]
            checks.append(
                BuildStep(name="typecheck", argv=_exec_argv(root, tsc), parse_diagnostics=True)
            )
        for script in ("build", "lint", "test"):
            if _has_script(root, script):
                checks.append(
                    BuildStep(
                        name=script,
                        argv=[*_runner(root), "run", script],
                        parse_diagnostics=script == "build",
                    )
                )
        return BuildPlan(
            install=BuildStep(name="install", argv=_install_argv(root)),
            checks=checks,
        )

    async def resolve_graph(self, sandbox: Sandbox, root: ProjectRoot) -> DependencyGraph:
        if root.package_manager is PackageManager.NPM:
            argv = ["npm", "ls", "--all", "--json"]
        elif root.package_manager is PackageManager.PNPM:
            argv = ["corepack", "pnpm", "list", "--json", "--depth", "Infinity"]
        else:
            raise UnsupportedProject(f"{root.path}: Yarn projects are not supported yet")

        result = await sandbox.exec(
            argv, cwd=root.path, timeout_s=300, max_output=_MAX_GRAPH_OUTPUT
        )
        if result.truncated:
            raise AdapterError(f"{root.path}: dependency tree is too large to read")
        # npm ls exits non-zero for problems like unmet peers, but still prints the tree.
        try:
            data = json.loads(result.output)
        except json.JSONDecodeError as error:
            raise AdapterError(
                f"{root.path}: could not read the dependency tree: {result.output[-500:]}"
            ) from error
        return parse_graph(data)

    def dependency_paths(self, graph: DependencyGraph, pkg: PackageId) -> list[DepPath]:
        dependencies: defaultdict[PackageId, list[PackageId]] = defaultdict(list)
        dependents: defaultdict[PackageId, list[PackageId]] = defaultdict(list)
        for dependent, dependency in graph.edges:
            dependencies[dependent].append(dependency)
            dependents[dependency].append(dependent)

        # Only walk packages that can lead to the target, so large graphs stay fast.
        useful = set([pkg])
        queue = deque([pkg])
        while queue:
            for dependent in dependents[queue.popleft()]:
                if dependent not in useful:
                    useful.add(dependent)
                    queue.append(dependent)
        if graph.root not in useful:
            return []

        # Shortest chains come first, because they are the clearest evidence.
        paths: list[DepPath] = []
        chains: deque[DepPath] = deque([[graph.root]])
        while chains and len(paths) < _MAX_PATHS:
            chain = chains.popleft()
            if chain[-1] == pkg:
                paths.append(chain)
                continue
            for dependency in dependencies[chain[-1]]:
                if dependency in useful and dependency not in chain:
                    chains.append([*chain, dependency])
        return paths

    def parse_diagnostics(self, output: str) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for line in output.splitlines():
            match = _TSC_ERROR.match(line)
            if match:
                diagnostics.append(
                    Diagnostic(
                        file=match["file"],
                        line=int(match["line"]),
                        column=int(match["column"]),
                        severity=match["severity"],
                        code=match["code"],
                        message=match["message"],
                    )
                )
        return diagnostics


def parse_graph(data: Any) -> DependencyGraph:
    """Turn `npm ls --json` (one object) or `pnpm list --json` (a list) into a graph."""
    if isinstance(data, list):
        items = cast(list[Any], data)
        if not items:
            raise AdapterError("dependency tree is empty")
        data = items[0]
    if not isinstance(data, dict):
        raise AdapterError("dependency tree has an unexpected shape")
    top = cast(dict[str, Any], data)

    root = PackageId(name=str(top.get("name", "(root)")), version=str(top.get("version", "0.0.0")))
    packages: set[PackageId] = set()
    edges: set[tuple[PackageId, PackageId]] = set()

    pending: list[tuple[PackageId, dict[str, Any]]] = [(root, top)]
    while pending:
        parent, node = pending.pop()
        for key in _DEPENDENCY_KEYS:
            children = cast(dict[str, Any], node.get(key) or {})
            for name, child in children.items():
                details = cast(dict[str, Any], child)
                version = details.get("version")
                if not version or details.get("missing"):
                    continue  # Declared but not installed, such as an unmet peer dependency.
                package = PackageId(name=name, version=str(version))
                packages.add(package)
                edges.add((parent, package))
                pending.append((package, details))

    return DependencyGraph(
        root=root,
        packages=sorted(packages, key=str),
        edges=sorted(edges, key=lambda edge: (str(edge[0]), str(edge[1]))),
    )


def _read_package_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        logger.warning("skipping unreadable %s: %s", path, error)
        return None
    if not isinstance(data, dict):
        logger.warning("skipping %s: it is not a JSON object", path)
        return None
    return cast(dict[str, Any], data)


def _project_root(
    relative: PurePosixPath, folder: Path, files: set[str], manifest: dict[str, Any]
) -> ProjectRoot:
    raw_scripts = cast(dict[str, Any], manifest.get("scripts") or {})
    declared = {
        **cast(dict[str, Any], manifest.get("dependencies") or {}),
        **cast(dict[str, Any], manifest.get("devDependencies") or {}),
    }
    return ProjectRoot(
        path=str(relative),
        package_manager=_package_manager(files, manifest),
        has_lockfile=bool(files & {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}),
        has_typescript="typescript" in declared and (folder / "tsconfig.json").exists(),
        scripts={name: cmd for name, cmd in raw_scripts.items() if isinstance(cmd, str)},
    )


def _package_manager(files: set[str], manifest: dict[str, Any]) -> PackageManager:
    if "pnpm-lock.yaml" in files:
        return PackageManager.PNPM
    if "yarn.lock" in files:
        return PackageManager.YARN
    if "package-lock.json" in files:
        return PackageManager.NPM

    declared = str(manifest.get("packageManager", ""))
    if declared.startswith("pnpm@") or "pnpm-workspace.yaml" in files:
        return PackageManager.PNPM
    if declared.startswith("yarn@"):
        return PackageManager.YARN
    return PackageManager.NPM


def _has_script(root: ProjectRoot, name: str) -> bool:
    script = root.scripts.get(name)
    # `npm init` writes a placeholder test script that always fails. It isn't a real test.
    return bool(script) and "no test specified" not in (script or "")


def _runner(root: ProjectRoot) -> list[str]:
    return ["npm"] if root.package_manager is PackageManager.NPM else ["corepack", "pnpm"]


def _exec_argv(root: ProjectRoot, command: list[str]) -> list[str]:
    if root.package_manager is PackageManager.NPM:
        return ["npx", "--no-install", *command]
    return ["corepack", "pnpm", "exec", *command]


def _install_argv(root: ProjectRoot) -> list[str]:
    if root.package_manager is PackageManager.PNPM:
        frozen = ["--frozen-lockfile"] if root.has_lockfile else []
        return ["corepack", "pnpm", "install", *frozen, "--ignore-scripts"]
    command = "ci" if root.has_lockfile else "install"
    return ["npm", command, "--ignore-scripts"]
