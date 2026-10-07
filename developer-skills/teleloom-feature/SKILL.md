---
name: teleloom-feature
description: Implement or extend MCP capabilities in the teleloom repository using its existing daemon, Telegram adapters, durable jobs and public MCP/CLI tests. Use for repository development, not for reading or sending Telegram messages during ordinary agent sessions.
---

# Add a teleloom capability

Work from the repository root containing `AGENTS.md` and the `teleloom`
`pyproject.toml`. Repository instructions and the accepted task define scope.

1. Read `AGENTS.md`, `GLOSSARY.md`, the relevant issue and ADR. Distinguish
   accepted requirements from proposals in `docs/roadmap.md`. Assign the issue
   before implementation, following `docs/agents/issue-tracker.md`.
2. Read [the extension map](references/implementation.md). Write a compact
   feature contract: example request, inputs, outputs, capabilities, coverage,
   limits, failure behavior and observable completion. Resolve routine choices
   from existing code; ask only about a missing decision that changes scope.
3. Choose the nearest public test seam: MCP, CLI or an observable skill workflow.
   Add one failing behavior test, implement that behavior end to end, then
   refactor. Fake external APIs and clocks; use real temporary SQLite and queues.
   Do not build all internal layers first or test only private helpers.
4. Extend the existing adapter/runtime/job path. Use the repository's ID, date,
   cursor, profile isolation and error helpers. Keep credentials and runtime
   connections under their existing owner. Add a job when work cannot fit a
   bounded request. Read the job guidance in the extension map when applicable.
5. If the task authorizes subagents, read
   [coordination](references/coordination.md) and assign exclusive file ownership.
   Otherwise implement the slice yourself. Delegation does not authorize live
   Telegram mutations or changes to profile permissions.
6. Update `docs/reference.md`, domain/ADR documentation when semantics change,
   and the affected runtime skills under `skills/`. Keep this developer skill
   under `developer-skills/`; it is not a seventh bundled runtime skill.
7. Run the checks below. Record actual live observations separately from fake-API
   tests in `docs/acceptance.md`. New MCP tools need client reconnection because
   discovery metadata is cached by the stdio bridge.

Before committing, run the affected public behavior checks, especially delivery,
confirmation and permissions when touched. Apply Ruff/format to edited Python;
mypy, skill validation and packaging checks only when their contracts change.
From the repository root, read `docs/agents/implementation-standards.md` and
`docs/test-loop.md`: one full pytest run before a substantial release unless the
owner cancels it; no nested or repeated full runs.

Finish when the user-visible workflow passes through the supported interfaces,
limits and partial failures are explicit, compatibility is preserved, and the
issue has reproducible evidence. Report unperformed live/client checks honestly.
If a release is requested, follow the existing release procedure: one build,
artifact identity and owner-state preservation. Record any cancelled check as a
waiver. Independent reviews apply when requested by the task/owner. A package
build does not establish live Telegram acceptance.
