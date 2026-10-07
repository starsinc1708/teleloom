# Test loops and release costs

## Check policy

Follow [implementation standards](agents/implementation-standards.md#completion-and-release-evidence)
for affected checks, the one full run before a substantial release, one canonical
build and owner-state preservation. This document supplies concrete commands;
it does not add another completion gate bundle.

`uv run pytest` remains the complete offline suite, including real release
preparation, installed inventory, HTTP/stdio and owner replacement. Preparation
does not recursively launch pytest. Defaults remain two xdist workers, loadgroup
and strict markers. Full test CI is manual and uses standard GitHub-hosted Ubuntu; PRs run lightweight checks. Empty `test_targets` means one Python 3.12 full run; space-separated
`tests/` selectors run only the affected cases without interpreting shell commands.
The `compatibility` input adds Python 3.13/3.14 only for an explicitly requested
compatibility check. The default workflow does not claim Windows acceptance.
The `deployment` input builds one wheel/
sdist pair, verifies identity without rebuilding, and reuses the wheel for installed,
desktop and Docker checks. Neither option publishes a release or upgrades an owner.

## CI runner operations

The [workflow](../.github/workflows/ci.yml) uses standard GitHub-hosted Ubuntu
with read-only permissions and no Telegram credentials or private owner data.
Pull requests run lightweight lint/type/skill checks. Manual runs on `main`
select affected `tests/` cases; an empty selection runs one full suite.
Compatibility and deployment checks remain explicit inputs. Public fork code
does not execute on a private LAN/self-hosted runner.

```text
gh workflow run ci.yml --repo starsinc1708/teleloom --ref main -f test_targets="tests/test_cli.py" -f compatibility=false -f deployment=false
gh run list --repo starsinc1708/teleloom --workflow ci.yml --limit 5
```

Record the actual run URL, source and selected checks. Standard hosted runner
minutes are free for public repositories; larger runners/storage have separate
billing. [GitHub billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Local development checks

For a **shortened development loop**, name the affected contract explicitly:

```text
uv run pytest -n 0 tests/test_read_policy.py tests/test_projection.py --durations=0
uv run pytest -n 0 tests/test_reading_sdk_v02.py tests/test_threads_v02.py tests/test_rich_parity.py --durations=0
uv run pytest -n 0 -m "not release_e2e and not process_e2e"
```

Media contracts are selected by `tests/test_media_security.py`,
`tests/test_media_display.py` and `tests/test_media_delivery.py`; administration
by `tests/test_administration_read.py` and `tests/test_administration_management.py`.
`docs/test-selectors.json` maps moved selectors. Parameter cases stay intact.

The marker command deliberately omits expensive contracts. It is not a full run.
`release_e2e` identifies release/owner workflows; `process_e2e` identifies subprocess
workflows. Neither marker changes default selection or stands in for xdist groups.

A targeted release case builds one genuine stamped package and collects its
isolated installed inventory, without a nested suite or behavioral smoke:

```text
uv run pytest -n 0 tests/test_release_owner.py::test_active_jobs_block_owner_replacement --durations=0
```

Keep mutable owner installations, SQLite, ports and lock directories per test.
The two release modules share their single session-scoped `real_release` on one
xdist worker. `RELEASE_TEST_FIXTURE` is not a source-validated cross-run cache;
omit it from actual completion checks and measurements.

Use `--basetemp` to retain timing evidence outside the checkout. Each real release
has `fixture-timings.json` (copy/init), `prepare-timings.json` (clone, single build,
metadata and installed inventory), and
`previous-wheel-timings.json`. Each owner case has `owner-target-timings.json`,
including actual cleanup. The original preparation snapshot remains available when
a subsequent reuse rewrites `prepare-report.json`; reuse is not a new build. Stage
records contain completion exit, perf_counter seconds and source/build identities.
`workers` describes the outer pool for fixture stages; preparation stages use zero
xdist workers. Child processes are counted separately from pytest workers.
They exclude commands, environments, credentials, content and exception dumps.
Installed inventory records environment/install/probe/cleanup costs.

Only run repeated timing comparisons when performance measurement is requested.
Compare the same OS, Python patch, cases, workers, cache state and background load;
state sample sizes and source differences. Do not turn benchmarks into routine gates.

## chigwell comparison (2026-10-06)

At upstream `c4f9b238c65ebe98bcd920cb2dd89947c4cf57c2`, the
[test workflow](https://github.com/chigwell/telegram-mcp/blob/c4f9b238c65ebe98bcd920cb2dd89947c4cf57c2/.github/workflows/tests.yml)
runs one `pytest --cov` on Ubuntu/Python 3.11 with dummy Telegram settings.
[Lint/format](https://github.com/chigwell/telegram-mcp/blob/c4f9b238c65ebe98bcd920cb2dd89947c4cf57c2/.github/workflows/python-lint-format.yml)
uses separate Black/Flake8 checks;
[Docker/Compose](https://github.com/chigwell/telegram-mcp/blob/c4f9b238c65ebe98bcd920cb2dd89947c4cf57c2/.github/workflows/docker-build.yml)
has a separate workflow. These workflows run on main/PR/manual events.
There is no nested release pytest in those workflows. The 80% coverage threshold
is configured for `main`, `sanitize` and `telegram_mcp.runtime`, not all adapters.

We retain public MCP/CLI and real SQLite/state tests for our owner/confirmation
contracts, while removing repeated full checks from artifact preparation and
download verification. See [the release procedure](release-pipeline.md).
