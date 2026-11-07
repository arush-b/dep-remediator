# Remediator

Finds vulnerable dependencies, upgrades them, fixes the code that breaks, proves the result in a sandbox, and opens a reviewable pull request.

Early development. See [the design doc](dependency-remediation-design.md) for the full plan.

**Only run repositories you trust.** The sandbox does not yet have the full isolation the design calls for.

## Setup

You need [uv](https://docs.astral.sh/uv/) and a running Docker engine.

```
uv sync
```

## Tests

```
uv run pytest                       # everything, including tests that start containers
uv run pytest -m "not docker"       # skip the container tests
uv run pyright                      # type checks
uv run ruff check .                 # lint
```
