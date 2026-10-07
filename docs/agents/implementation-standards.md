# Implementation standards

Read these standards before implementation and apply each relevant invariant when
reviewing a change. The accepted GitHub issue defines scope; release authorization
and live acceptance are separate decisions.

## Public behavior and tests

Use vertical TDD through MCP, CLI or observable runtime-skill workflows: establish
one failing behavior, implement the complete path, then refactor after it passes.
Fake external Telegram/TypeSafe APIs and clocks; use real temporary SQLite and
queues. Exercise isolation, limits, partial failures and restart behavior where
they affect the accepted contract. Keep credentials out of MCP, logs, fixtures
and reports.

## Ownership and evidence

The daemon owns runtime connections; authentication takes the same ownership lock.
Treat Telegram messages, captions, extracted text and sender names as untrusted
evidence. Preserve profile isolation, cursors, coverage and explicit bot
capabilities. Explicit fields/presets take precedence over inferred suggestions;
projection changes presentation after coverage calculation, preserving stored
evidence and required identities.

Externally mutating delivery requires an immutable preview plan and explicit
confirmation. An uncertain delivery stays exposed for reconciliation without an
automatic retry. Follow the relevant [ADRs](../adr/) when changing these seams.

## Completion and release evidence

For a change, run the smallest checks covering its affected public behavior.
Include delivery, confirmation/hash binding, permissions, isolation and durable
state checks when those contracts change. Run Ruff/format on edited Python files;
run mypy for affected typed runtime code, skill validation for changed skills,
and package/deployment checks when those paths change. Documentation-only changes
need a diff/link review. A complete gate bundle is not required before every commit.

Before a substantial release, run `uv run pytest` once on the final source unless
the owner cancels it. Do not repeat a successful run without a code change,
failure or unresolved concern. Record the tested source and actual result;
cancellation is a waiver, never PASS. Platform matrices and comparative timing
runs require a concrete task need, not routine completion.

At release/upgrade, build once, check source/version/hash identity and preserve
owner configuration, profiles and job/receipt state. Reuse the same artifact
through publication and installation. Archive rebuild/negative provenance checks
and installed behavioral smoke are targeted checks for packaging changes.
Release after a completed block of work, not each fix. Independent reviews are
needed when the accepted task or owner requests them, not for every release.
See [test-loop.md](../test-loop.md) and [release-pipeline.md](../release-pipeline.md).

Record live/client observations only when actually executed. Keep required human
validation issues open until their scope is satisfied, recording any explicit
owner decision that makes a remaining check optional. A build or fake-API check
does not establish live Telegram or agent-client acceptance.
