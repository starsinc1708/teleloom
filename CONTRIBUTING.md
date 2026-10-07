# Contributing to Teleloom

Start with a [bug report or proposal](https://github.com/starsinc1708/teleloom/issues).
Describe the user workflow and expected behavior; keep credentials, session
material and private chat content out of public reports. Security reports belong
in [private vulnerability reporting](SECURITY.md#report-a-vulnerability).

## Development setup

```sh
git clone https://github.com/starsinc1708/teleloom.git
cd teleloom
uv sync --frozen --all-extras
```

Python 3.12–3.14 is supported. If this checkout also hosts an installed owner,
select a separate `UV_PROJECT_ENVIRONMENT` before syncing. Never replace a live
owner with editable development code to run checks.

Coding agents read [AGENTS.md](AGENTS.md). The
[implementation standards](docs/agents/implementation-standards.md) define the
public CLI/MCP test seams and safety rules. Fake Telegram/AI; use real temporary
SQLite and queues. Keep permissions, confirmation/hash binding, profile isolation,
coverage and unknown delivery outcomes intact.

## Verify the change

Run the smallest tests that exercise changed behavior, plus applicable Ruff,
format, mypy and skill checks. See [test selection](docs/test-loop.md).
A substantial release gets one full suite unless explicitly waived by the owner;
documentation-only changes need a diff/link check. Record what was executed and
its limits; a skipped check is not a pass.

Open a PR against `main`, link the accepted issue, explain the resulting behavior,
and report relevant validation. Small complete changes are easier to review than
unrelated refactors. Public PR checks use ephemeral GitHub-hosted runners with
read-only permissions; no private owner state, Telegram credentials or LAN runner.

For packaging changes read [ADR 0005](docs/adr/0005-build-provenance.md) and
[release procedure](docs/release-pipeline.md). Build once and reuse the verified
artifact. Native client and live Telegram observations belong in
[acceptance](docs/acceptance.md) only when actually performed.

MIT license; contributions retain that license. The [roadmap](docs/roadmap.md)
separates implemented capabilities from conditional proposals.
