# Remediator: Multi-Agent Dependency and Vulnerability Remediation

**Design document** · Status: Draft v2 · Working name: Remediator

---

## Table of contents

1. [Summary](#1-summary)
2. [Problem statement](#2-problem-statement)
3. [Goals and non-goals](#3-goals-and-non-goals)
4. [Users and usage modes](#4-users-and-usage-modes)
5. [System overview](#5-system-overview)
6. [Remediation workflow](#6-remediation-workflow)
7. [Agent specifications](#7-agent-specifications)
8. [Deterministic analysis components](#8-deterministic-analysis-components)
9. [Knowledge layer (RAG)](#9-knowledge-layer-rag)
10. [Tooling layer and MCP](#10-tooling-layer-and-mcp)
11. [Sandbox and build execution](#11-sandbox-and-build-execution)
12. [Autonomy model and policy](#12-autonomy-model-and-policy)
13. [Authentication, authorization and multi-tenancy](#13-authentication-authorization-and-multi-tenancy)
14. [Security and guardrails](#14-security-and-guardrails)
15. [LLM gateway and LLMOps](#15-llm-gateway-and-llmops)
16. [Observability](#16-observability)
17. [Evaluation](#17-evaluation)
18. [Data model](#18-data-model)
19. [Configuration](#19-configuration)
20. [Deployment](#20-deployment)
21. [Technology choices](#21-technology-choices)
22. [Milestones](#22-milestones)
23. [Risks and mitigations](#23-risks-and-mitigations)
24. [Open questions](#24-open-questions)
25. [Glossary](#25-glossary)

---

## 1. Summary

Remediator is an open-source system that fixes vulnerable and outdated dependencies end to end. It does more than report them. For each advisory affecting a repository it:

1. determines whether the vulnerable code is actually reachable from the project,
2. plans the safest upgrade that resolves it,
3. performs the upgrade and rewrites code broken by API changes,
4. proves the result builds and passes the same tests as before, in an isolated sandbox,
5. opens a reviewable pull request with the evidence behind every decision.

The work is split between deterministic analysis (dependency resolution, API diffing, reachability graphs, builds) and a team of specialized LLM agents for the parts that need judgment (interpreting changelogs, adapting code, reviewing changes). Launch ecosystems are **TypeScript (npm and pnpm)** first and **Java (Maven)** second, behind an ecosystem adapter interface so others can be added. Remediator itself is written in Python.

It ships as a GitHub App, a GitHub Action, and a CLI, and is fully self-hostable with any LLM provider, including local models.

---

## 2. Problem statement

Dependency maintenance fails in two predictable ways.

**Alert fatigue.** Scanners flag every advisory that matches a version range, regardless of whether the project calls the vulnerable code. Teams receive far more alerts than they can act on, cannot tell which ones matter, and start ignoring all of them.

**Stalled upgrades.** Tools like Dependabot and Renovate open version-bump PRs, but when the new version contains breaking API changes, the PR fails CI and waits for a human. Those PRs accumulate. The vulnerable version stays in production.

The expensive part of remediation is not detecting the problem or changing a version number. It is the investigation and adaptation in between: reading changelogs, finding affected call sites, rewriting code, and convincing a reviewer the change is safe. That is the part Remediator automates.

### Existing landscape

| Tool category | What it does | Gap Remediator fills |
|---|---|---|
| Version bump bots (Dependabot, Renovate) | Open PRs that change version numbers | Do not fix breaking changes; failed PRs stall |
| Advisory scanners (npm audit, OWASP Dependency-Check, OSV-Scanner) | Match dependencies against advisories | No reachability, no fixes |
| Commercial SCA platforms | Reachability and prioritization | Mostly closed source; limited automated code adaptation |
| Deterministic refactoring (OpenRewrite) | Recipe-based migrations | Only covers changes someone wrote a recipe for |

Remediator consumes the output of scanners and advisory databases, and can call deterministic recipes where they exist. It focuses on reachability, adaptation and verification.

---

## 3. Goals and non-goals

### Goals

- **G1. Accurate prioritization.** Classify each advisory as reachable, unreachable or unknown, with an evidence trail.
- **G2. Green upgrades.** Produce upgrade PRs that build and pass the project's existing tests, including when the upgrade requires code changes.
- **G3. Reviewability.** Every PR explains what changed, why, and how it was verified, so a maintainer can review it in minutes.
- **G4. Safety by default.** No change reaches a default branch without the configured approvals. Untrusted code and text never gain control of the system.
- **G5. Measurable quality.** Every claim about quality is backed by a reproducible evaluation suite run in CI.
- **G6. Easy adoption.** Install as a GitHub App in one step, or run fully self-hosted with a single command.
- **G7. Cost control.** Bounded, observable cost per remediation, with budgets enforced per installation.

### Non-goals

- Replacing vulnerability scanners or advisory databases. Remediator consumes them.
- Generating exploits or proofs of concept for vulnerabilities.
- Auto-merging major-version upgrades.
- Supporting every ecosystem at launch. TypeScript first, then Java with Maven; Gradle, Yarn, plain JavaScript, Python and .NET later through the adapter interface. Go and Rust are not currently planned.
- Fixing vulnerabilities in the project's own first-party code (only dependency-induced issues).

---

## 4. Users and usage modes

### Personas

| Persona | Need |
|---|---|
| Open-source maintainer | Fewer noisy alerts; upgrade PRs that are ready to merge |
| Application team engineer | Clear signal on which advisories matter; less upgrade toil |
| Security engineer / AppSec | Org-wide view of exposure, reachability evidence, remediation SLAs |
| Platform team | Self-hosted deployment, policy control, model choice, cost visibility |

### Usage modes

| Mode | Trigger | Output |
|---|---|---|
| **GitHub App** (primary) | Webhooks, nightly schedule, `/remediator` PR or issue comments | Check runs, PRs, issue comments, dashboard |
| **GitHub Action** | Workflow step in the repo's own CI | Report artifact, optional PR |
| **CLI** | `remediator scan`, `remediator fix <advisory>` locally | Terminal report, local branch with changes |

All three share the same core engine. The App adds multi-tenancy, a dashboard and persistent state.

---

## 5. System overview

```mermaid
flowchart TB
    subgraph Ingress
        WH[Webhook receiver]
        SCH[Scheduler]
        CLI[CLI / Action]
    end

    subgraph ControlPlane[Control plane]
        API[API server]
        WF[Durable workflow engine]
        POL[Policy engine]
        DB[(Postgres + pgvector)]
    end

    subgraph AgentRuntime[Agent runtime]
        ORCH[Coordinator]
        AG[Specialist agents]
    end

    subgraph Analysis[Deterministic analysis]
        ECO[Ecosystem adapters]
        REACH[Reachability engine]
        APID[API diff engine]
        ADV[Advisory sync]
    end

    subgraph Exec[Execution]
        SBX[Sandbox pool]
    end

    GW[LLM gateway]
    KB[Knowledge layer]
    OBS[Telemetry pipeline]
    UI[Web dashboard]
    GH[GitHub API]

    WH --> WF
    SCH --> WF
    CLI --> WF
    API --> DB
    UI --> API
    WF --> ORCH
    ORCH --> AG
    AG --> GW
    AG --> KB
    AG --> ECO
    AG --> REACH
    AG --> APID
    AG --> SBX
    ORCH --> POL
    ADV --> DB
    KB --> DB
    WF --> DB
    ORCH --> GH
    GW -.-> OBS
    WF -.-> OBS
    SBX -.-> OBS
```

### Component responsibilities

| Component | Responsibility |
|---|---|
| Webhook receiver | Verifies GitHub signatures (`X-Hub-Signature-256`), normalizes events, enqueues workflows. Never runs analysis inline. |
| Scheduler | Nightly rescans and advisory-driven rescans when new advisories arrive. |
| Durable workflow engine | Persists every remediation as a state machine; resumes after crashes; enforces timeouts, retries and budgets. |
| Policy engine | Evaluates every side-effecting action (push, open PR, merge, comment) against tenant and repository policy. |
| Coordinator | Drives a remediation through its stages, dispatches agents, aggregates results. Mostly deterministic code. |
| Specialist agents | LLM-driven roles with narrow tools and narrow context (see section 7). |
| Ecosystem adapters | Parse manifests and lockfiles, resolve dependency graphs, apply version changes, run builds for each ecosystem. |
| Reachability engine | Builds symbol reference graphs and finds paths from project entry points to vulnerable symbols. |
| API diff engine | Computes public API differences between two versions of a library. |
| Advisory sync | Mirrors advisories from OSV.dev (which aggregates GitHub Security Advisories and others). |
| Knowledge layer | Indexed changelogs, migration guides, API docs and past remediations for retrieval. |
| Sandbox pool | Isolated environments for cloning, building and testing untrusted code. |
| LLM gateway | Single path for all model calls: routing, budgets, caching, redaction, logging. |
| Web dashboard | Findings, runs, approvals, costs and links to traces. |

### Design principles

1. **Deterministic first, LLM second.** Anything a compiler, resolver or diff tool can answer is answered by that tool. LLMs handle interpretation and adaptation only.
2. **Evidence or it didn't happen.** Every verdict (reachable, safe, verified) carries machine-checkable evidence attached to the run.
3. **Narrow agents.** Each agent sees only the context and tools its role needs.
4. **Side effects behind policy.** No agent calls GitHub or pushes code directly. Agents propose actions; the coordinator executes them after policy checks.
5. **Fail toward humans.** When stuck or uncertain, stop with a useful partial result and a clear explanation, never with a forced or speculative fix.

---

## 6. Remediation workflow

Each remediation is a durable state machine, one per (repository, advisory or upgrade target).

```mermaid
stateDiagram-v2
    [*] --> Detected
    Detected --> Triaged
    Triaged --> Suppressed: duplicate / accepted risk
    Triaged --> ReachabilityAssessed
    ReachabilityAssessed --> Reported: unreachable (policy: report only)
    ReachabilityAssessed --> Planned: reachable / unknown / policy: fix all
    Planned --> BaselineVerified
    BaselineVerified --> Escalated: baseline build broken
    BaselineVerified --> Bumped
    Bumped --> Verified: build + tests match baseline
    Bumped --> Migrating: breaking changes
    Migrating --> Verified: fixed within budget
    Migrating --> Escalated: budget or iteration limit hit
    Verified --> Reviewed
    Reviewed --> Migrating: reviewer rejects
    Reviewed --> PROpened
    PROpened --> Merged
    PROpened --> Closed
    Merged --> [*]
    Closed --> [*]
    Reported --> [*]
    Suppressed --> [*]
    Escalated --> [*]
```

### Stage details

| Stage | What happens | Primary actor |
|---|---|---|
| Detected | New advisory matches a resolved dependency, or a scheduled scan finds one. | Advisory sync + ecosystem adapter |
| Triaged | Duplicates merged (same advisory via several dependency paths), priority score computed. | Triage agent |
| ReachabilityAssessed | Verdict with evidence: reachable (path found), unreachable (no path, high confidence), unknown. | Reachability engine + reachability analyst |
| Planned | Target version and strategy selected (lockfile-only, direct bump, transitive override). | Upgrade planner |
| BaselineVerified | Build and tests run on the unchanged base commit to record the baseline, including pre-existing failures and flaky tests. | Verifier |
| Bumped | Version change applied; build and tests run. | Ecosystem adapter + verifier |
| Migrating | Breaking changes researched and code adapted in an edit, build, diagnose loop. | Migration researcher + code-change agents + verifier |
| Verified | Results match or improve on baseline. | Verifier |
| Reviewed | Adversarial review of the diff; PR description written. | Reviewer agent |
| PROpened | PR created per autonomy level (draft or ready). | Coordinator (after policy check) |
| Escalated | Partial work pushed to a draft PR or issue with diagnosis, if policy allows. | Coordinator |

### Budgets and limits per remediation

| Limit | Default | Purpose |
|---|---|---|
| Wall-clock time | 45 minutes | Bound runaway jobs |
| Migration iterations | 6 edit-build cycles | Prevent endless loops |
| LLM spend | $2.00 | Cost control (configurable per tenant) |
| Files changed | 40 | Keep PRs reviewable; larger changes escalate |
| Sandbox CPU / memory | 4 vCPU / 8 GB | Isolation and fairness |

Budget exhaustion is a normal terminal outcome (`Escalated`), not an error.

---

## 7. Agent specifications

The coordinator is mostly deterministic code. The six specialist agents below use LLMs. Each has a fixed tool allowlist, a context budget and a structured output schema validated before the coordinator accepts it.

### Agent roster

```mermaid
flowchart LR
    C[Coordinator] --> T[Triage]
    C --> R[Reachability analyst]
    C --> P[Upgrade planner]
    C --> M[Migration researcher]
    C --> E1[Code-change agent 1]
    C --> E2[Code-change agent N]
    C --> V[Verifier]
    C --> RV[Reviewer]
```

### 7.1 Coordinator

- **Role:** Owns the state machine. Dispatches agents, merges results, applies policy, executes side effects.
- **LLM use:** Minimal. Used only to summarize run outcomes for humans; all control flow is code.
- **Why not an LLM orchestrator:** Control flow must be auditable, resumable and testable. LLM judgment is applied inside stages, not between them.

### 7.2 Triage agent

| Field | Value |
|---|---|
| Inputs | Advisory records, dependency paths, repository metadata, previous decisions for this repo |
| Tools | `advisory.get`, `deps.paths`, `history.decisions` |
| Output | Deduplicated finding list with priority score and rationale |
| Model tier | Small |

**Priority score** combines: severity (CVSS), exploit likelihood (EPSS where available), whether the dependency is direct or transitive, reachability (once known), runtime vs build-only vs test-only scope, and estimated upgrade difficulty (from the API diff). The formula is deterministic; the agent writes the rationale and resolves ambiguous cases such as an advisory affecting only an optional feature.

### 7.3 Reachability analyst

| Field | Value |
|---|---|
| Inputs | Engine output (paths, unresolved edges), advisory text, vulnerable symbol list |
| Tools | `reach.query`, `reach.path`, `repo.read`, `repo.search` |
| Output | Verdict (`reachable`, `unreachable`, `unknown`), confidence, evidence (paths or reasons), human-readable explanation |
| Model tier | Large |

The engine does the graph work (section 8.2). The agent handles what static analysis cannot settle alone: interpreting advisories that describe vulnerable behavior rather than naming functions, reasoning about reflection or dynamic dispatch, and deciding whether a feature flag or configuration makes a path live. It may only downgrade a verdict to `unreachable` when the engine found no path **and** no unresolved edges touch the relevant code. This asymmetry is deliberate: a false "unreachable" hides real risk.

### 7.4 Upgrade planner

| Field | Value |
|---|---|
| Inputs | Dependency graph, fixing versions from advisory, version constraints, API diff summary |
| Tools | `deps.graph`, `deps.resolve_dry_run`, `apidiff.summary`, `registry.versions` |
| Output | Target version, strategy, expected breaking-change scope, alternatives considered |
| Model tier | Medium |

**Strategies**, in order of preference:

1. **Lockfile-only.** Update the locked version within existing constraints (`npm update <package>` or `pnpm update <package>`; for Maven, a version within the declared range where ranges are used).
2. **Direct bump.** Change the version in the manifest (`package.json`, `pom.xml`).
3. **Transitive override.** Force a transitive dependency's version (`overrides` in npm, `pnpm.overrides` in pnpm, `<dependencyManagement>` in Maven) when the intermediate dependency has not released a fix.
4. **Intermediate upgrade.** Upgrade the direct dependency that pulls in the vulnerable transitive one.

The planner prefers the smallest version that fixes the advisory over the latest version, unless policy says otherwise, and runs a resolver dry run before committing to a plan.

### 7.5 Migration researcher

| Field | Value |
|---|---|
| Inputs | API diff, compile errors from the bumped build, usage sites in the repo |
| Tools | `apidiff.detail`, `kb.search`, `repo.search`, `repo.read` |
| Output | **Breaking-change brief**: list of affected symbols, old usage, new equivalent, source citations, affected files |
| Model tier | Large |

The brief is the contract between research and editing. Each entry cites where the mapping came from (API diff, changelog line, migration guide section). Entries without a source are marked `inferred` and receive extra reviewer scrutiny. Briefs for public library transitions are cached and reused across tenants (section 15.4).

### 7.6 Code-change agents

| Field | Value |
|---|---|
| Inputs | Breaking-change brief entries for its assigned files, compiler diagnostics for those files |
| Tools | `repo.read`, `repo.edit` (restricted to assigned files), `repo.search` |
| Output | Patch for assigned files with per-change rationale |
| Model tier | Large |
| Parallelism | One agent per module or package; files partitioned so agents never edit the same file |

Code-change agents cannot run builds or touch git. The verifier runs builds and returns structured diagnostics; the coordinator routes each diagnostic back to the agent owning that file. Edits outside assigned files are rejected by the tool layer.

### 7.7 Verifier

| Field | Value |
|---|---|
| Inputs | Workspace snapshot, baseline results |
| Tools | `sandbox.build`, `sandbox.test`, `sandbox.lint` |
| Output | Structured result: compile diagnostics, test results per test, comparison against baseline |
| Model tier | Small (only for summarizing large failure output) |

The verifier is mostly deterministic. A change is **verified** when:

- the build succeeds,
- every test that passed on the baseline still passes,
- no tests were removed, skipped or newly ignored,
- linters report no new errors (warnings are reported, not blocking).

Flaky tests are detected by rerunning baseline failures up to three times and recorded separately.

### 7.8 Reviewer agent

| Field | Value |
|---|---|
| Inputs | Full diff, breaking-change brief, verification results, advisory |
| Tools | `repo.read`, `diff.view`, `apidiff.detail` |
| Output | Approve or reject with findings; PR title and description |
| Model tier | Large (different model or prompt family from code-change agents where available) |

The reviewer acts as an adversary to the code-change agents. It checks for:

- changes outside the scope of the upgrade,
- silent behavior changes (changed defaults, error handling, timeouts, serialization formats),
- weakened tests (modified assertions, added `.skip`, `.only` or `@Disabled`, deleted test files),
- weakened types (added `any`, `as` casts, `@ts-ignore` or `@ts-expect-error` that silence a compiler error instead of fixing it),
- `inferred` brief entries that lack justification,
- new transitive dependencies, install scripts or Maven plugins introduced by the upgrade (supply-chain signal),
- PR description claims not supported by the run's evidence.

A rejection returns the run to `Migrating` with findings, counted against the iteration budget.

### Agent communication

Agents never talk to each other directly. All exchange goes through typed artifacts stored on the run (finding, verdict, plan, brief, patch, verification result, review). This keeps runs replayable, prevents chatter loops, and makes every handoff inspectable in traces and the dashboard.

---

## 8. Deterministic analysis components

### 8.1 Ecosystem adapters

Each ecosystem implements one interface:

```python
class EcosystemAdapter(Protocol):
    def detect(self, repo: Workspace) -> list[ProjectRoot]: ...
    async def resolve_graph(self, sandbox: Sandbox, root: ProjectRoot) -> DependencyGraph: ...
    def dependency_paths(self, graph: DependencyGraph, pkg: PackageId) -> list[DepPath]: ...
    async def apply_change(self, sandbox: Sandbox, root: ProjectRoot, change: VersionChange) -> ChangeSet: ...
    async def dry_run_resolve(self, sandbox: Sandbox, root: ProjectRoot, change: VersionChange) -> ResolveOutcome: ...
    def build_commands(self, root: ProjectRoot) -> BuildPlan: ...
    def parse_diagnostics(self, output: BuildOutput) -> list[Diagnostic]: ...
    def scip_index_command(self, root: ProjectRoot) -> Command: ...
```

Operations that run package managers or builds are async and take a sandbox handle, because they are long-running and must never run on the host.

| Concern | TypeScript (npm, pnpm) | Java (Maven) |
|---|---|---|
| Graph resolution | `npm ls --all --json`, `pnpm list --json`, plus the lockfile | `mvn dependency:tree` (with effective POM) |
| Workspaces / modules | npm and pnpm workspaces | Multi-module reactor builds |
| Version change | `package.json` edit, lockfile update, `overrides` | `pom.xml` edit, `<dependencyManagement>`, property-based versions |
| Build / test | `npm ci` or `pnpm install --frozen-lockfile`, `tsc --noEmit`, the project's build and test scripts | `mvn -B verify` |
| Diagnostics | `tsc` output parsing, Jest or Vitest JSON reports | Compiler output parsing, Surefire XML reports |
| Dependency scope | `dependencies`, `devDependencies`, `peerDependencies`, `optionalDependencies` | Scopes (`compile`, `runtime`, `test`, `provided`), profiles |

Manifest edits preserve formatting and comments (format-preserving JSON and XML editing), because noisy reformatting makes PRs hard to review.

### 8.2 Reachability engine

**Approach:** symbol reference graphs from SCIP indexes, a language-neutral code intelligence format.

- TypeScript: `scip-typescript`
- Java: `scip-java`

**Steps:**

1. **Index** the project and the resolved dependency versions in the sandbox.
2. **Identify vulnerable symbols:**
   - from the advisory when it lists affected functions (uncommon for npm and Maven advisories, so the fix commit is expected to be the main source),
   - otherwise from the **fix commit** referenced by the advisory: symbols whose bodies changed in the fix are candidate vulnerable symbols,
   - otherwise mark the advisory `symbol-unknown` (verdict can be at best `unknown`).
3. **Build the graph:** nodes are symbols; edges are references from inside one symbol's body to another symbol.
4. **Find entry points:** `main` methods and `bin` scripts, public API of libraries (`exports` and `main` in `package.json`, public Java classes), test functions (tracked separately), framework entry points (Express and Next.js route handlers, Spring controllers and beans, servlet handlers, annotated main classes).
5. **Search** for paths from entry points to vulnerable symbols; return the shortest few as evidence.
6. **Record unresolved edges:** values typed `any`, dynamic property access, dynamic `import()` and `require`, objects that satisfy an interface structurally without declaring it, untyped JavaScript in dependencies, Java dynamic dispatch, reflection, service loaders and dependency-injection wiring.

**Conservative over-approximation:** dynamic dispatch edges connect to all implementations known in the graph (class hierarchy analysis). This produces some false "reachable" verdicts but avoids false "unreachable" ones. Test-only reachability is reported distinctly from production reachability.

**Output example:**

```json
{
  "advisory": "GHSA-xxxx-xxxx-xxxx",
  "verdict": "reachable",
  "confidence": "high",
  "paths": [
    ["src/server.ts:main", "src/routes/upload.ts:handleUpload", "image-lib:decodeTiff", "tiff-lib:readStrip"]
  ],
  "unresolved_edges_near_target": 0,
  "scope": "production"
}
```

### 8.3 API diff engine

Computes the public API change between the current and target versions of a library, before any LLM is involved.

| Ecosystem | Tooling |
|---|---|
| TypeScript | Comparison of the published `.d.ts` type declarations of the two versions (from the package or `@types`); packages without types fall back to changelog research and are marked lower confidence |
| Java | Bytecode API comparison of the two JARs (`japicmp` or `revapi`) |

The result is a structured list of removed, renamed, changed-signature and deprecated symbols. It is intersected with the project's symbol references to produce the set of **affected usage sites** before migration begins. This keeps the migration researcher focused on symbols the project actually uses.

### 8.4 Advisory sync

- Source of truth: OSV.dev data (aggregating GitHub Security Advisories and other databases), mirrored locally.
- Incremental sync every hour; new advisories trigger rescans of affected installations.
- Stored fields: identifiers and aliases, affected ranges, fixed versions, severity, references (fix commits), and ecosystem-specific affected-function data where present.
- Advisory text is treated as **untrusted input** (section 14).

---

## 9. Knowledge layer (RAG)

### Corpora

| Corpus | Source | Scope | Refresh |
|---|---|---|---|
| Changelogs and release notes | Repository `CHANGELOG` files, GitHub releases | Public, shared | On new release |
| Migration guides | Project docs, upgrade guides | Public, shared | On new release |
| API docs | Package type declarations and doc comments, Javadoc | Public, shared | Per version |
| Advisory text | OSV records | Public, shared | Hourly |
| Remediation history | Completed runs, accepted briefs, reviewer findings, merged diffs | **Tenant-private** | On run completion |
| Repository code | Tenant repositories | **Tenant-private**, per run | Per run (ephemeral) |

### Ingestion

- Markdown and HTML are parsed into sections with headings preserved.
- Chunks follow document structure (one release section, one migration step, one API item), not fixed token windows. Each chunk carries metadata: library, version or version range, section path, source URL, ingestion date.
- Changelog sections are tagged with the version they describe so retrieval can filter to the versions between current and target.

### Retrieval

- **Hybrid search:** Postgres full-text search (BM25-style ranking) combined with pgvector similarity, merged with reciprocal rank fusion.
- **Filters first:** library and version range filters are applied before ranking, so a query for "express 4.x to 5.0" never surfaces notes from unrelated versions.
- **Reranking** of the top candidates with a cross-encoder or small LLM before they enter agent context.
- **Tenant isolation:** private corpora live in tenant-scoped tables; every retrieval query carries a tenant ID enforced at the database layer through row-level security, not only in application code.

### Retrieval quality targets

| Metric | Target |
|---|---|
| Recall@10 for the correct changelog section, given a breaking symbol | ≥ 0.90 |
| Version-filter violations (chunks from outside the requested range) | 0 |
| Cross-tenant retrieval leaks | 0 (tested in CI) |

---

## 10. Tooling layer and MCP

All agent tools are defined once in a typed tool registry with JSON schemas, permission tags and side-effect classification.

| Tool | Side effect | Available to |
|---|---|---|
| `advisory.get` | None | Triage, reachability analyst |
| `deps.graph`, `deps.paths`, `deps.resolve_dry_run` | None | Triage, planner |
| `reach.query`, `reach.path` | None | Reachability analyst |
| `apidiff.summary`, `apidiff.detail` | None | Planner, researcher, reviewer |
| `kb.search` | None | Researcher |
| `repo.read`, `repo.search` | None | Most agents |
| `repo.edit` | Workspace write (sandboxed, file-scoped) | Code-change agents |
| `sandbox.build`, `sandbox.test`, `sandbox.lint` | Compute only | Verifier |
| `diff.view` | None | Reviewer |
| `github.*` (branch, commit, PR, comment) | **External** | Coordinator only, after policy check |

The same registry is exposed as an **MCP server**, so maintainers can use Remediator's analysis tools (reachability queries, API diffs, remediation history) from any MCP-capable client. The MCP surface is read-only by default; write tools require an explicitly configured token with the corresponding policy permissions.

---

## 11. Sandbox and build execution

Builds execute arbitrary code: npm install scripts (`preinstall`, `postinstall`), bundler and test-runner plugins, Maven plugins and test code. Every build is treated as hostile. Install scripts are disabled by default (`--ignore-scripts`) and enabled only when a project needs them, for example to compile native modules.

| Control | Implementation |
|---|---|
| Isolation | One sandbox per run; containers under gVisor (`runsc`); Firecracker microVMs as an optional backend for stronger isolation |
| Network | Default deny. Egress only through a proxy that allowlists package registries (npm registry, Maven Central, configured private registries) |
| Secrets | No credentials inside sandboxes. Private registry access goes through the proxy, which injects credentials outside the sandbox |
| Filesystem | Fresh workspace from a clone; dependency caches mounted read-only and shared per ecosystem to speed builds |
| Resources | CPU, memory, disk and process limits; hard wall-clock timeout |
| Lifecycle | Destroyed after the run; artifacts (logs, test reports, diffs) copied out and stored |

**Build caching:** per-tenant warm caches (npm cache, pnpm store, Maven `~/.m2`) are snapshotted by lockfile hash and reused for the baseline build. Cache contents are never shared across tenants because build outputs can embed private code.

---

## 12. Autonomy model and policy

### Autonomy levels

Configured per repository, and optionally per change type.

| Level | Behavior |
|---|---|
| `report` | Findings and reachability verdicts only, as check runs or a dashboard |
| `draft_pr` | Opens draft PRs; humans mark them ready |
| `ready_pr` | Opens ready-for-review PRs |
| `auto_merge` | Merges automatically, only for change types allowed by policy (default: lockfile-only patch-level upgrades with green CI and an approving reviewer agent) |

Major-version upgrades can never be auto-merged, regardless of configuration.

### Policy engine

Policies are written in **Cedar** and evaluated in-process through Python bindings, behind a small interface so the engine can be replaced if needed. Every side-effecting action is a policy request with principal, action, resource and context.

```cedar
// Allow auto-merge only for lockfile-only patch upgrades with green verification.
permit (
    principal == Remediator::Agent::"coordinator",
    action == Remediator::Action::"merge_pr",
    resource in Remediator::Repo::"acme/payments-api"
)
when {
    context.change_type == "lockfile_only" &&
    context.semver_delta == "patch" &&
    context.verification == "passed" &&
    context.reviewer_verdict == "approve"
};

// Never push to a default branch.
forbid (
    principal,
    action == Remediator::Action::"push",
    resource
)
when { context.target_branch_is_default };
```

Policy decisions, inputs and outcomes are written to the audit log.

---

## 13. Authentication, authorization and multi-tenancy

### GitHub App authentication

- The App authenticates with its private key (stored in a secrets manager or KMS, never on disk in plaintext) to mint **installation access tokens**: short-lived and scoped to the installation's repositories.
- Tokens are minted per run, scoped further to the single repository being remediated, and never passed into sandboxes or LLM context.

**Requested App permissions (minimum):**

| Permission | Level | Reason |
|---|---|---|
| Metadata | Read | Required baseline |
| Contents | Read & write | Clone, push remediation branches |
| Pull requests | Read & write | Open and update PRs |
| Checks | Read & write | Report findings as check runs; read CI status |
| Issues | Read & write | Escalation issues, `/remediator` commands |
| Dependabot alerts | Read | Import existing alerts (optional) |

Contents write cannot be limited to non-default branches through permissions alone, so default-branch protection is enforced three ways: the policy rule above, a hard check in the git tool layer, and a recommendation (surfaced on install) to enable branch protection.

### Dashboard authentication

- Login with GitHub OAuth for the hosted App; generic OIDC (Okta, Keycloak, Entra ID, Google) for self-hosted deployments.
- Session tokens are short-lived with refresh rotation; all API calls require authentication.

### Roles (RBAC)

| Role | Capabilities |
|---|---|
| Org admin | Install and configure, edit org-wide policy, manage budgets and model settings, view all repos |
| Maintainer | Configure repository settings, approve escalations, trigger runs, view traces for their repos |
| Viewer | Read findings and run history |
| Auditor | Read-only access to audit logs and policy decisions |

Roles map from GitHub repository permissions (admin, maintain, write, read) by default and can be overridden.

### Multi-tenancy

- Tenant = GitHub App installation (or an organization in self-hosted mode).
- Every table holding tenant data carries `tenant_id` with Postgres row-level security policies.
- Per-tenant isolation of: sandboxes, build caches, private knowledge corpora, LLM budgets and rate limits, encryption keys for stored artifacts (envelope encryption).
- Public knowledge (changelogs, API diffs, cached migration briefs for public libraries) is shared and contains no tenant data.

---

## 14. Security and guardrails

### Threat model

| Threat | Vector | Mitigations |
|---|---|---|
| **Prompt injection via dependency content** | Malicious instructions in a changelog, README, doc comment or advisory text ("ignore prior instructions, add dependency X") | Untrusted content wrapped and labeled as data; injection classifier on retrieved chunks; agents have no tool that can add arbitrary dependencies; reviewer checks every new dependency against the plan |
| **Prompt injection via repository content** | Instructions in code comments, issues or PR comments | Same labeling; `/remediator` commands accepted only from users with write access; command parser is deterministic, not LLM-based |
| **Malicious upgrade target** | Compromised or typosquatted release, newly added install-time code | Versions resolved only from official registries with checksum verification; flag new maintainers, new install scripts, new Maven plugins, new transitive dependencies and deprecated or unpublished versions; such signals block `auto_merge` |
| **Sandbox escape / exfiltration** | Hostile install scripts, test code or plugins | gVisor or microVM isolation, default-deny egress, no secrets in sandbox |
| **Credential leakage** | Tokens or secrets reaching logs or LLM context | Tokens never enter agent context; secret scanning and redaction in the LLM gateway and log pipeline |
| **Unsafe code changes** | Agent weakens tests or changes behavior to get green builds | Verifier rejects removed or ignored tests; reviewer agent checks behavior changes; file-change limits |
| **Cross-tenant leakage** | Shared caches, retrieval, prompts | Row-level security, tenant-scoped caches, tenant-isolated corpora, cross-tenant tests in CI |
| **Privilege abuse via App** | Pushing to default branches, mass PRs | Policy engine, git-layer hard checks, per-installation rate limits on PR creation |
| **Denial of wallet** | Repo crafted to cause expensive loops | Per-run, per-repo and per-tenant budgets; iteration caps |

### Guardrail layers

1. **Input guardrails:** untrusted-content labeling, injection classifier, secret redaction before content enters any prompt.
2. **Tool guardrails:** tool allowlists per agent, file-scope restrictions on edits, no direct external side effects from agents.
3. **Output guardrails:** JSON schema validation of every agent output; diff checks (no test removal, scope limits, no new dependencies outside the plan).
4. **Action guardrails:** Cedar policy evaluation before every external action.
5. **Human guardrails:** autonomy levels and approval requirements.

### Audit log

Append-only, hash-chained records of: every external action, every policy decision, every agent output accepted by the coordinator, and every human approval or override. Exportable for compliance review.

---

## 15. LLM gateway and LLMOps

### 15.1 Gateway responsibilities

All model calls go through the gateway; no agent calls a provider directly.

- **Provider adapters:** Anthropic, OpenAI, Google, and any OpenAI-compatible endpoint (vLLM, Ollama, LiteLLM) for self-hosted models.
- **Routing:** by model tier (small, medium, large) mapped to concrete models in config.
- **Fallbacks:** on provider errors or rate limits, retry with backoff, then fall back to the configured secondary model for that tier.
- **Budgets:** per run, per repository, per tenant; enforced before each call.
- **Rate limiting:** per tenant and per provider key.
- **Redaction:** secret and token patterns removed from prompts and responses.
- **Recording:** prompt template version, model, tokens, latency, cost, cache hits; full payload capture optional and off by default for private repositories.

### 15.2 Model tiers

| Tier | Used by | Characteristics |
|---|---|---|
| Small | Triage, verifier summaries | Cheap and fast, structured output |
| Medium | Upgrade planner | Moderate reasoning |
| Large | Reachability analyst, migration researcher, code-change agents, reviewer | Strongest available reasoning and coding |

### 15.3 Prompt management

- Prompts are versioned files in the repository (`prompts/<agent>/<version>.md`) with typed variables.
- Each run records the prompt versions used.
- Any prompt or model change runs the evaluation gate (section 17.5) before merge.
- Prompt structure places stable content (role instructions, tool definitions, schemas) first to maximize provider-side prompt caching.

### 15.4 Caching

| Cache | Key | Scope |
|---|---|---|
| Provider prompt cache | Stable prompt prefix | Provider-managed |
| API diff results | (library, from version, to version) | Global (public data) |
| Migration briefs | (library, from version, to version, brief schema version) | Global for public libraries; covers symbol mappings only, never tenant code |
| Reachability indexes for dependency versions | (package, version, indexer version) | Global |
| Retrieval results | Query hash + filters | Per tenant, short TTL |

Cached migration briefs mean the second project upgrading the same library between the same versions pays a fraction of the research cost.

### 15.5 Cost targets

| Remediation type | Target median LLM cost |
|---|---|
| Lockfile-only, no code changes | < $0.05 |
| Direct bump, no code changes | < $0.15 |
| Upgrade requiring code migration | < $1.00 |

---

## 16. Observability

### Tracing

OpenTelemetry throughout, using the OpenTelemetry GenAI semantic conventions for model calls.

```
remediation.run                     (tenant, repo, advisory, final_state)
├── stage.triage
│   └── agent.triage
│       └── gen_ai.chat             (gen_ai.request.model, gen_ai.usage.input_tokens, output_tokens, cost)
├── stage.reachability
│   ├── tool.scip_index             (duration, symbols)
│   ├── tool.reach_query
│   └── agent.reachability_analyst
├── stage.plan
├── stage.baseline                  (sandbox.id, build.duration, tests.total, tests.failed)
├── stage.migrate
│   ├── iteration.1
│   │   ├── agent.code_change[module=core]
│   │   ├── agent.code_change[module=api]
│   │   └── tool.sandbox_build
│   └── iteration.2 ...
├── stage.review
└── stage.publish                   (policy.decision, pr.number)
```

Traces export to any OTLP backend (Grafana Tempo, Jaeger); LLM spans can additionally be sent to LLM-specific tools such as Langfuse or Arize Phoenix. The dashboard links each run to its trace.

### Metrics

| Metric | Type | Labels |
|---|---|---|
| `remediation_runs_total` | Counter | tenant, ecosystem, final_state |
| `remediation_duration_seconds` | Histogram | ecosystem, strategy |
| `remediation_llm_cost_usd` | Histogram | tenant, ecosystem, strategy |
| `llm_tokens_total` | Counter | agent, model, direction |
| `llm_calls_failed_total` | Counter | provider, reason |
| `migration_iterations` | Histogram | ecosystem |
| `reachability_verdicts_total` | Counter | verdict |
| `sandbox_build_duration_seconds` | Histogram | ecosystem, cache_hit |
| `policy_denials_total` | Counter | action, rule |
| `guardrail_triggers_total` | Counter | layer, type |
| `prs_merged_total`, `prs_closed_unmerged_total` | Counter | strategy |

### Service level objectives

| SLO | Target |
|---|---|
| Webhook acknowledgement | p99 < 2 s |
| Time to PR, lockfile-only | p95 < 10 min |
| Time to PR, with migration | p95 < 45 min |
| Run success rate excluding budget escalations | ≥ 99% (no crashes or stuck runs) |

### Logging

Structured JSON logs with run and trace IDs; secret redaction applied in the pipeline; payload logging of prompts disabled by default for private repositories.

### Dashboards

- **Operations:** queue depth, run durations, failures by stage, sandbox utilization.
- **Cost:** spend by tenant, agent and model; cache hit rates; budget burn.
- **Quality:** merge rate, time to merge, revert rate, escalation reasons, reviewer rejection rate.
- **Security:** guardrail triggers, policy denials, injection detections.

---

## 17. Evaluation

Evaluation is a first-class subsystem with its own datasets, harness and CI gates. Results are published in the repository.

### 17.1 Upgrade benchmark (end to end)

**Dataset construction:**

1. Mine public TypeScript and Java repositories for upgrade PRs (bot-opened or human) where the version bump alone failed CI and a later commit made it pass.
2. For each case record: base commit, upgraded dependency, versions, the human fix (ground truth) and the test command.
3. Filter to cases that build reproducibly in the sandbox.
4. Split into **dev** (used while iterating) and **held-out** (run only for releases) sets, stratified by ecosystem and change size.
5. Include a lockfile-only control set where no code changes are needed.

**Metrics:**

| Metric | Definition |
|---|---|
| Resolve rate | Share of cases ending `Verified` with tests matching baseline |
| False-green rate | Cases marked verified that fail an extended hidden test suite or differ semantically from ground truth on review |
| Diff size ratio | Lines changed relative to the human fix |
| Reviewer acceptance | Share of results a human grader would merge (sampled, rubric-based) |
| Cost and duration | Median and p90 per case |
| Escalation quality | For unresolved cases, whether the diagnosis correctly identified the blocker (rubric-graded) |

### 17.2 Reachability benchmark

- Labeled set of (project, advisory) pairs with ground truth established by manual analysis, and for a subset by dynamic confirmation (running the project's tests with instrumentation on the vulnerable symbol).
- Metrics: precision and recall per verdict, with emphasis on the **false unreachable rate**, the most dangerous error. Target: below 2%.
- Coverage: share of advisories with identifiable vulnerable symbols.

### 17.3 Component evals

| Component | Eval |
|---|---|
| Triage | Deduplication accuracy; priority ranking agreement with expert ordering (rank correlation) |
| Upgrade planner | Choice of minimal fixing version; resolver success of the plan |
| Migration researcher | Precision and recall of symbol mappings against the human fix; citation accuracy |
| Retrieval | Recall@k of correct changelog sections; version-filter violations |
| Reviewer | Detection rate on a seeded set of bad diffs (weakened tests, behavior changes, out-of-scope edits, unplanned dependencies) |

### 17.4 Security evals

- **Injection corpus:** changelogs, READMEs, doc comments, advisories and issue comments containing injected instructions (direct, encoded, split across chunks, disguised as migration steps). Metric: attack success rate, target 0% for any unplanned side effect.
- **Sandbox escape tests:** builds attempting network access to non-allowlisted hosts, filesystem escapes and resource exhaustion.
- **Tenant isolation tests:** attempts to retrieve or cache-hit another tenant's data.

### 17.5 CI gates

| Trigger | Suite | Gate |
|---|---|---|
| Every PR to Remediator | Unit and integration tests; fast eval subset (~30 cases); injection corpus; isolation tests | No regression beyond tolerance; zero injection successes |
| Prompt or model config change | Full dev eval set | Resolve rate within −2 percentage points of main, cost within +15% |
| Nightly | Full dev set + reachability benchmark | Tracked on dashboard |
| Release | Held-out set | Published in release notes |

LLM-as-judge graders (for reviewer acceptance and escalation quality) are calibrated against a human-labeled sample, and agreement is reported alongside results.

### 17.6 Online evaluation

| Signal | Meaning |
|---|---|
| Merge rate of opened PRs | Primary usefulness signal |
| Human edit distance before merge | How much maintainers had to change |
| Time to merge | Reviewability |
| Revert rate within 30 days | Hidden breakage |
| Explicit feedback | Thumbs up/down and reasons on PRs, fed into the remediation history corpus |

---

## 18. Data model

Core tables (Postgres; all tenant-scoped tables use row-level security):

| Table | Key fields |
|---|---|
| `tenants` | id, github_installation_id, plan, budget_limits, encryption_key_ref |
| `repositories` | id, tenant_id, full_name, default_branch, ecosystems, autonomy_config |
| `advisories` | id, aliases, ecosystem, package, affected_ranges, fixed_versions, severity, vulnerable_symbols, references (global) |
| `findings` | id, tenant_id, repo_id, advisory_id, dependency_paths, priority_score, status |
| `reachability_results` | id, finding_id, verdict, confidence, evidence_paths, unresolved_edges, engine_version |
| `remediation_runs` | id, tenant_id, finding_id, state, strategy, target_version, budgets_used, prompt_versions, trace_id |
| `run_artifacts` | id, run_id, kind (plan, brief, patch, verification, review), content_ref, schema_version |
| `policy_decisions` | id, run_id, action, decision, rule_ids, context_hash |
| `audit_log` | seq, tenant_id, actor, action, payload_hash, prev_hash, timestamp |
| `kb_chunks` | id, tenant_id (nullable for public), corpus, library, version_range, section_path, text, embedding, source_url |
| `brief_cache` | library, from_version, to_version, schema_version, brief, created_at (global) |
| `eval_runs` | id, suite, git_sha, prompt_versions, model_config, metrics, created_at |

Large artifacts (logs, diffs, test reports) are stored in object storage, encrypted per tenant, and referenced by `content_ref`.

---

## 19. Configuration

### Repository configuration (`.remediator.yml`)

```yaml
version: 1

autonomy:
  default: draft_pr
  overrides:
    lockfile_only_patch: auto_merge
    major_upgrade: report

scope:
  ecosystems: [npm, pnpm, maven]
  include_paths: ["services/**"]
  exclude_packages: ["internal-legacy-*"]
  dependency_scopes: [runtime, build]   # skip test-only advisories

prioritization:
  minimum_severity: medium
  require_reachable: true               # unreachable findings are reported, not fixed

limits:
  max_files_changed: 40
  max_llm_cost_usd: 2.00
  max_open_prs: 5

verification:
  build_command: default
  test_command: "pnpm -r test"
  flaky_test_retries: 3

reviewers:
  request: ["@acme/platform-team"]
  labels: ["dependencies", "security"]
```

### Deployment configuration (environment)

| Variable | Purpose |
|---|---|
| `REMEDIATOR_DATABASE_URL` | Postgres connection |
| `REMEDIATOR_GITHUB_APP_ID`, `REMEDIATOR_GITHUB_PRIVATE_KEY_REF` | App identity (key from secrets manager) |
| `REMEDIATOR_MODEL_TIERS` | Mapping of tiers to provider models |
| `REMEDIATOR_PROVIDER_KEYS_REF` | Provider credentials reference |
| `REMEDIATOR_SANDBOX_BACKEND` | `gvisor` or `firecracker` |
| `REMEDIATOR_REGISTRY_ALLOWLIST` | Egress allowlist for sandboxes |
| `REMEDIATOR_OTLP_ENDPOINT` | Telemetry export |
| `REMEDIATOR_OIDC_*` | Dashboard identity provider (self-hosted) |

---

## 20. Deployment

### Topologies

| Topology | Components | Use |
|---|---|---|
| **CLI / Action** | Python package (installed with `uvx` or `pipx`) for the CLI; Docker image for the Action; local Docker for sandbox; no database (state in run directory) | Individual repos, CI pipelines |
| **Single node** | `docker compose`: API + workers, Postgres, sandbox runner, OTel collector, optional Grafana stack | Small teams, evaluation, demos |
| **Kubernetes** | Helm chart: API, workflow workers, sandbox node pool (gVisor RuntimeClass), Postgres (external recommended), object storage, OTel collector | Organizations, hosted App |

### Demo environment

`docker compose --profile demo up` starts the full system against a set of bundled sample repositories with known vulnerable and outdated dependencies, so a new user can watch a complete remediation, including reachability evidence, migration and the resulting PR, without connecting their own repositories.

### Scaling

- API and webhook receiver: stateless, horizontally scaled.
- Workflow workers: scale on queue depth.
- Sandbox pool: dedicated node pool; autoscaled; the main cost driver alongside LLM spend.
- Postgres: single primary with read replicas; pgvector sufficient for expected corpus sizes (low millions of chunks).

---

## 21. Technology choices

| Area | Choice | Rationale |
|---|---|---|
| Core services, CLI, gateway, analysis | Python (FastAPI, SQLAlchemy, Alembic, Pydantic), packaged with `uv` | Fast iteration on agent and prompt logic; mature LLM tooling; heavy work runs in external tools and the sandbox. Strict `pyright` and `ruff` enforced in CI, with Pydantic models at every component boundary |
| Graph analysis | `rustworkx` | Fast graph search from Python; performance-critical parts can move to a native extension if benchmarks require it |
| GitHub integration | `githubkit` | Typed GitHub API client for Python |
| Policy | Cedar (`cedarpy` bindings) | Analyzable policy language, in-process evaluation |
| Supply chain of Remediator itself | Dependencies pinned with hashes | The service holds GitHub credentials, so its own dependencies must be tightly controlled |
| Workflow durability | Postgres-backed state machine and job queue | No extra infrastructure for self-hosters; Temporal considered (see open questions) |
| Database and vectors | Postgres + pgvector + full-text search | One datastore for state, hybrid retrieval and RLS-based isolation |
| Code intelligence | SCIP (`scip-typescript`, `scip-java`) | Language-neutral symbol graphs |
| API diff | `.d.ts` declaration comparison; `japicmp` / `revapi` | Deterministic, ecosystem-native |
| Sandbox | Docker + gVisor; Firecracker optional | Strong isolation with practical operations |
| Telemetry | OpenTelemetry (traces, metrics, logs) | Vendor-neutral; GenAI conventions |
| Dashboard | TypeScript, React | Standard web stack |
| Eval harness | Python | Data analysis and grading ecosystem; calls the system through the CLI and API |
| License | Apache-2.0 | Permissive; compatible with enterprise adoption |

---

## 22. Milestones

Each milestone ends with a usable release and published evaluation results.

### M0: Foundations
- Workspace, CI, telemetry from day one, durable state machine skeleton.
- Run state stored in files behind a small store interface. Postgres is added when a milestone first needs it, at the latest in M3 for the knowledge layer.
- Ecosystem adapter for TypeScript (npm and pnpm): graph resolution and builds in the sandbox.
- **Exit:** CLI resolves dependencies and runs baseline builds in the sandbox for sample repositories, fully traced.

### M1: Deterministic upgrades
- Advisory sync, finding detection, triage scoring, lockfile-only and direct-bump strategies, verifier with baseline comparison.
- Draft PR creation through the coordinator with policy checks.
- **Exit:** green PRs for upgrades needing no code changes; lockfile-only control set at ≥ 95% resolve rate.

### M2: Reachability
- SCIP indexing, vulnerable symbol extraction (advisory data and fix commits), path search, reachability analyst agent.
- Reachability benchmark v1.
- **Exit:** verdicts with evidence on findings; false unreachable rate measured and below 5%.

### M3: Migration agents
- API diff engine, knowledge layer ingestion and hybrid retrieval, migration researcher, parallel code-change agents, edit-build loop with budgets.
- Upgrade benchmark v1 (dev set) and CI eval gate.
- **Exit:** published resolve rate on the dev set for upgrades requiring code changes.

### M4: Review, guardrails and policy
- Reviewer agent, diff guardrails, injection defenses and corpus, Cedar policies, autonomy levels, audit log.
- **Exit:** zero injection successes on the corpus; reviewer detection rate published.

### M5: GitHub App and multi-tenancy
- App installation flow, webhooks, `/remediator` commands, RBAC, row-level security, per-tenant budgets, dashboard, OIDC for self-hosting.
- **Exit:** multiple tenants running concurrently with isolation tests passing; Helm chart and compose deployment documented.

### M6: Second ecosystem and launch
- Java (Maven) ecosystem adapter, held-out benchmark run, MCP server, demo environment, documentation, public release.
- **Exit:** v1.0 with published benchmark results for both ecosystems.

---

## 23. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Reachability false negatives hide real risk | Medium | High | Conservative over-approximation; asymmetric verdict rules; `unknown` as a first-class verdict; benchmark gating |
| Agents produce green but subtly wrong changes | Medium | High | Baseline comparison, no-test-weakening rules, adversarial reviewer, conservative autonomy defaults, revert-rate tracking |
| Builds not reproducible in sandbox (private registries, system dependencies, network-dependent tests) | High | Medium | Registry proxy, configurable build images, `verification` overrides, clear escalation reason |
| LLM cost exceeds value for small upgrades | Medium | Medium | Deterministic paths for lockfile-only and simple bumps; brief caching; budgets |
| Prompt injection through dependency content | Medium | High | Layered guardrails; agents lack dangerous tools; injection corpus in CI |
| Maintainers distrust automated PRs | Medium | High | Draft-first defaults, evidence-rich PR descriptions, small diffs, easy per-repo controls |
| Benchmark contamination (models trained on the human fixes) | Medium | Medium | Held-out set built from fixes after model training cutoffs; refresh held-out set over time |
| GitHub API rate limits at scale | Medium | Medium | Per-installation queuing, conditional requests, webhook-driven updates instead of polling |
| Scope creep into a general coding agent | Medium | Medium | Non-goals enforced in design review; tools restricted to upgrade-related edits |
| Weak typing lets breakage pass verification (heavy `any` use, thin test suites in TypeScript projects) | High | High | Reviewer checks for weakened types; report type and test coverage signals on the PR; projects with weak signals are held back from `auto_merge` |
| Python too slow or memory-heavy on large SCIP indexes | Medium | Medium | Benchmark early on a large repository; move only the affected component to a native extension if needed |

---

## 24. Open questions

1. **Workflow engine.** Postgres-backed state machine (simpler to self-host) versus Temporal (richer durability and tooling, extra infrastructure).
2. **Gradle support timing.** Large share of Java projects use Gradle; adapter complexity is higher than Maven.
3. **Hosted offering.** Run a hosted App for open-source repositories, or ship self-hosted only at v1?
4. **Telemetry from self-hosted installs.** Opt-in anonymous aggregate quality metrics to improve benchmarks, or none at all?
5. **Fix-commit symbol extraction quality.** npm and Maven advisories rarely name affected functions, so fix commits are the main source of vulnerable symbols. Whether changed-symbol heuristics are precise enough, or whether an LLM-assisted pass should narrow candidates.
6. **Deterministic recipe integration.** Whether to invoke OpenRewrite recipes for Java migrations before code-change agents in v1 or defer.
7. **Cedar bindings.** Whether `cedarpy` is well enough maintained to depend on, or whether policy evaluation should use another approach.
8. **Target validation.** Confirm the TypeScript-first decision with advisory counts per ecosystem from OSV and a sample of failed upgrade PRs.

---

## 25. Glossary

| Term | Meaning |
|---|---|
| Advisory | Published record of a vulnerability in a package version range (CVE, GHSA) |
| Baseline | Build and test results on the unchanged base commit |
| Breaking-change brief | Structured, cited mapping from old API usage to new, produced before code edits |
| Escalation | Terminal state where the system stops and hands a diagnosis to a human |
| Lockfile-only upgrade | Version change within existing manifest constraints; only the lockfile changes |
| Reachability | Whether a path exists from project entry points to a vulnerable symbol |
| SCIP | Language-neutral format for code intelligence indexes (symbols, definitions, references) |
| Transitive override | Forcing the version of an indirect dependency |
| Verified | Build succeeds and all baseline-passing tests still pass, with no tests removed or skipped |
