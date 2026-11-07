"""TypeScript support: saved tool output for the odd cases, real tools for the real formats."""

import json
import shutil
from pathlib import Path

import pytest

from remediator.adapters.base import AdapterError, EcosystemAdapter, UnsupportedProject
from remediator.adapters.typescript import TypeScriptAdapter, parse_graph
from remediator.models import DependencyGraph, PackageId, PackageManager, ProjectRoot
from remediator.sandbox import DockerSandbox

SAMPLE = Path(__file__).parent.parent / "samples" / "ts-sample"
adapter: EcosystemAdapter = TypeScriptAdapter()


def write_project(folder: Path, *extra_files: str, **manifest: object) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "package.json").write_text(json.dumps(manifest))
    for name in extra_files:
        (folder / name).write_text("")


def project(**fields: object) -> ProjectRoot:
    values: dict[str, object] = {"path": ".", "package_manager": PackageManager.NPM, **fields}
    return ProjectRoot.model_validate(values)


def step_names(root: ProjectRoot) -> list[str]:
    return [step.name for step in adapter.build_commands(root).checks]


# Finding projects


def test_detects_a_project_at_the_top(tmp_path: Path) -> None:
    write_project(
        tmp_path,
        "package-lock.json",
        "tsconfig.json",
        scripts={"test": "vitest"},
        devDependencies={"typescript": "5.0.0"},
    )

    assert adapter.detect(tmp_path) == [
        ProjectRoot(
            path=".",
            package_manager=PackageManager.NPM,
            has_lockfile=True,
            has_typescript=True,
            scripts={"test": "vitest"},
        )
    ]


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        ("package-lock.json", PackageManager.NPM),
        ("pnpm-lock.yaml", PackageManager.PNPM),
        ("yarn.lock", PackageManager.YARN),
        ("pnpm-workspace.yaml", PackageManager.PNPM),
    ],
)
def test_files_decide_the_package_manager(
    tmp_path: Path, file: str, expected: PackageManager
) -> None:
    write_project(tmp_path, file)

    assert adapter.detect(tmp_path)[0].package_manager is expected


def test_package_manager_field_is_used_without_a_lockfile(tmp_path: Path) -> None:
    write_project(tmp_path, packageManager="pnpm@9.0.0")

    root = adapter.detect(tmp_path)[0]

    assert root.package_manager is PackageManager.PNPM
    assert not root.has_lockfile


def test_typescript_needs_both_the_package_and_a_config(tmp_path: Path) -> None:
    write_project(tmp_path / "config-only", "tsconfig.json")
    write_project(tmp_path / "package-only", dependencies={"typescript": "5.0.0"})

    assert [root.has_typescript for root in adapter.detect(tmp_path)] == [False, False]


def test_workspace_members_belong_to_their_root(tmp_path: Path) -> None:
    write_project(tmp_path, workspaces=["packages/*"])
    write_project(tmp_path / "packages" / "a")

    assert [root.path for root in adapter.detect(tmp_path)] == ["."]


def test_separate_projects_are_found_separately(tmp_path: Path) -> None:
    write_project(tmp_path / "web", "package-lock.json")
    write_project(tmp_path / "api", "pnpm-lock.yaml")

    assert [root.path for root in adapter.detect(tmp_path)] == ["api", "web"]


def test_installed_and_built_folders_are_skipped(tmp_path: Path) -> None:
    write_project(tmp_path / "node_modules" / "lodash")
    write_project(tmp_path / "dist")

    assert adapter.detect(tmp_path) == []


def test_broken_package_json_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "package.json").write_text("{not json")
    (tmp_path / "list").mkdir()
    (tmp_path / "list" / "package.json").write_text("[]")
    write_project(tmp_path / "good")

    assert [root.path for root in adapter.detect(tmp_path)] == ["good"]


# Planning commands


def test_npm_plan_runs_every_check_in_order() -> None:
    root = project(
        has_lockfile=True,
        has_typescript=True,
        scripts={"test": "vitest", "lint": "eslint .", "build": "tsc"},
    )

    plan = adapter.build_commands(root)

    assert plan.install.argv == ["npm", "ci", "--ignore-scripts"]
    assert [step.name for step in plan.checks] == ["typecheck", "build", "lint", "test"]
    assert plan.checks[0].argv[:3] == ["npx", "--no-install", "tsc"]
    assert plan.checks[3].argv == ["npm", "run", "test"]


def test_pnpm_plan_uses_pnpm() -> None:
    root = project(
        package_manager=PackageManager.PNPM,
        has_lockfile=True,
        has_typescript=True,
        scripts={"test": "vitest"},
    )

    plan = adapter.build_commands(root)

    assert plan.install.argv == [
        "corepack",
        "pnpm",
        "install",
        "--frozen-lockfile",
        "--ignore-scripts",
    ]
    assert plan.checks[0].argv[:4] == ["corepack", "pnpm", "exec", "tsc"]
    assert plan.checks[1].argv == ["corepack", "pnpm", "run", "test"]


@pytest.mark.parametrize("manager", [PackageManager.NPM, PackageManager.PNPM])
def test_install_scripts_are_always_off(manager: PackageManager) -> None:
    for has_lockfile in (True, False):
        root = project(package_manager=manager, has_lockfile=has_lockfile)
        assert "--ignore-scripts" in adapter.build_commands(root).install.argv


def test_install_without_a_lockfile_does_not_require_one() -> None:
    assert adapter.build_commands(project()).install.argv[1] == "install"


def test_missing_scripts_are_skipped() -> None:
    assert step_names(project(scripts={"dev": "vite"})) == []


def test_npm_placeholder_test_script_is_not_a_test() -> None:
    placeholder = 'echo "Error: no test specified" && exit 1'

    assert step_names(project(scripts={"test": placeholder})) == []


def test_only_compiler_steps_parse_diagnostics() -> None:
    root = project(has_typescript=True, scripts={"build": "tsc", "test": "vitest"})

    parsed = {s.name: s.parse_diagnostics for s in adapter.build_commands(root).checks}

    assert parsed == {"typecheck": True, "build": True, "test": False}


def test_yarn_is_refused_clearly() -> None:
    with pytest.raises(UnsupportedProject, match="Yarn"):
        adapter.build_commands(project(package_manager=PackageManager.YARN))


# Reading the dependency tree


APP = PackageId(name="app", version="1.0.0")
A = PackageId(name="a", version="1.0.0")
B = PackageId(name="b", version="2.0.0")
C = PackageId(name="c", version="3.0.0")


def test_parses_npm_output() -> None:
    graph = parse_graph(
        {
            "name": "app",
            "version": "1.0.0",
            "dependencies": {
                "a": {"version": "1.0.0", "dependencies": {"b": {"version": "2.0.0"}}},
            },
        }
    )

    assert graph.root == APP
    assert graph.packages == [A, B]
    assert graph.edges == [(A, B), (APP, A)]


def test_parses_pnpm_output_and_its_dev_dependencies() -> None:
    graph = parse_graph(
        [
            {
                "name": "app",
                "version": "1.0.0",
                "dependencies": {"a": {"version": "1.0.0"}},
                "devDependencies": {"b": {"version": "2.0.0"}},
            }
        ]
    )

    assert graph.packages == [A, B]


def test_same_package_in_two_places_appears_once() -> None:
    graph = parse_graph(
        {
            "name": "app",
            "version": "1.0.0",
            "dependencies": {
                "a": {"version": "1.0.0", "dependencies": {"c": {"version": "3.0.0"}}},
                "b": {"version": "2.0.0", "dependencies": {"c": {"version": "3.0.0"}}},
            },
        }
    )

    assert graph.packages == [A, B, C]
    assert (A, C) in graph.edges
    assert (B, C) in graph.edges


def test_uninstalled_dependencies_are_left_out() -> None:
    graph = parse_graph(
        {
            "name": "app",
            "version": "1.0.0",
            "dependencies": {
                "unmet": {"required": "^1.0.0", "missing": True},
                "a": {"version": "1.0.0"},
            },
        }
    )

    assert graph.packages == [A]


def test_project_without_a_name_still_parses() -> None:
    graph = parse_graph({"dependencies": {"a": {"version": "1.0.0"}}})

    assert graph.root == PackageId(name="(root)", version="0.0.0")


@pytest.mark.parametrize("data", [[], "text", 3, None])
def test_unexpected_tree_shapes_are_refused(data: object) -> None:
    with pytest.raises(AdapterError):
        parse_graph(data)


# Explaining why a package is present


def graph_of(*edges: tuple[PackageId, PackageId]) -> DependencyGraph:
    return DependencyGraph(root=APP, packages=[], edges=list(edges))


def test_finds_every_path_shortest_first() -> None:
    graph = graph_of((APP, A), (A, B), (B, C), (APP, C))

    assert adapter.dependency_paths(graph, C) == [[APP, C], [APP, A, B, C]]


def test_unrelated_package_has_no_path() -> None:
    assert adapter.dependency_paths(graph_of((APP, A)), C) == []


def test_cycles_do_not_hang() -> None:
    graph = graph_of((APP, A), (A, B), (B, A), (B, C))

    assert adapter.dependency_paths(graph, C) == [[APP, A, B, C]]


def test_number_of_paths_is_capped() -> None:
    middles = [PackageId(name=f"m{i}", version="1.0.0") for i in range(50)]
    edges = [(APP, m) for m in middles] + [(m, C) for m in middles]

    assert len(adapter.dependency_paths(graph_of(*edges), C)) == 20


# Reading compiler errors


def test_parses_compiler_errors() -> None:
    output = (
        "src/index.ts(12,5): error TS2339: Property 'login' does not exist on type 'Client'.\n"
        "  Did you mean 'signIn'?\n"
        "lib/my file.ts(1,1): error TS1005: ';' expected.\n"
        "Some unrelated line\n"
    )

    first, second = adapter.parse_diagnostics(output)

    assert first.file == "src/index.ts"
    assert (first.line, first.column) == (12, 5)
    assert first.severity == "error"
    assert first.code == "TS2339"
    assert first.message == "Property 'login' does not exist on type 'Client'."
    assert second.file == "lib/my file.ts"


def test_clean_output_has_no_diagnostics() -> None:
    assert adapter.parse_diagnostics("Done in 1.2s\n") == []


# The sample repo, using the real tools. These catch wrong assumptions about real output.


def test_sample_repo_is_detected_as_npm_typescript() -> None:
    root = adapter.detect(SAMPLE)[0]

    assert root.package_manager is PackageManager.NPM
    assert root.has_lockfile
    assert root.has_typescript


@pytest.mark.docker
async def test_real_npm_tree_contains_the_vulnerable_package() -> None:
    root = adapter.detect(SAMPLE)[0]

    async with DockerSandbox(adapter.image, network=True, env=adapter.sandbox_env) as sandbox:
        await sandbox.copy_in(SAMPLE)
        installed = await sandbox.exec(adapter.build_commands(root).install.argv)
        assert installed.ok, installed.output
        await sandbox.disable_network()
        graph = await adapter.resolve_graph(sandbox, root)

    lodash = PackageId(name="lodash", version="4.17.20")
    assert graph.root == PackageId(name="ts-sample", version="1.0.0")
    assert lodash in graph.packages
    assert adapter.dependency_paths(graph, lodash) == [[graph.root, lodash]]


@pytest.mark.docker
async def test_real_pnpm_install_and_tree_work_offline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns("node_modules", "dist"))

    async with DockerSandbox(adapter.image, network=True, env=adapter.sandbox_env) as sandbox:
        await sandbox.copy_in(repo)
        imported = await sandbox.exec(["corepack", "pnpm", "import"])
        assert imported.ok, imported.output
        root = project(package_manager=PackageManager.PNPM, has_lockfile=True)
        installed = await sandbox.exec(adapter.build_commands(root).install.argv)
        assert installed.ok, installed.output
        await sandbox.disable_network()
        graph = await adapter.resolve_graph(sandbox, root)

    assert PackageId(name="lodash", version="4.17.20") in graph.packages
    assert PackageId(name="typescript", version="5.6.3") in graph.packages


@pytest.mark.docker
async def test_real_compiler_errors_are_parsed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo, ignore=shutil.ignore_patterns("node_modules", "dist"))
    (repo / "src" / "broken.ts").write_text('export const n: number = "text";\n')
    root = adapter.detect(repo)[0]
    plan = adapter.build_commands(root)
    typecheck = next(step for step in plan.checks if step.name == "typecheck")

    async with DockerSandbox(adapter.image, network=True, env=adapter.sandbox_env) as sandbox:
        await sandbox.copy_in(repo)
        installed = await sandbox.exec(plan.install.argv)
        assert installed.ok, installed.output
        await sandbox.disable_network()
        result = await sandbox.exec(typecheck.argv)

    assert not result.ok
    diagnostic = adapter.parse_diagnostics(result.output)[0]
    assert diagnostic.file == "src/broken.ts"
    assert diagnostic.line == 1
    assert diagnostic.code == "TS2322"
