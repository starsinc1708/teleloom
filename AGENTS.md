# Working on Teleloom

Use `teleloom` consistently for the product, Python package/module, CLI, MCP
server and local application namespace. Runtime skills use the `teleloom-` prefix.

Read [implementation standards](docs/agents/implementation-standards.md) before
implementation or review; they define public test seams, safety invariants and completion.

- Domain changes: read [GLOSSARY](GLOSSARY.md), [domain behavior](docs/agents/domain.md)
  and the relevant [ADR](docs/adr/) before changing a seam or security policy.
- Tasks and PRs: follow [the GitHub workflow](docs/agents/issue-tracker.md) and
  [triage labels](docs/agents/triage-labels.md); record scope and actual evidence.
- MCP capabilities: use [the feature skill](developer-skills/teleloom-feature/SKILL.md)
  and its extension map. Read its coordination reference when delegation is authorized.
- Tests/CI: select affected public behavior under the standards. Public CI uses
  GitHub-hosted Ubuntu; read [test loops](docs/test-loop.md) before dispatching checks.
- Releases/upgrades: read [release procedure](docs/release-pipeline.md); use one
  canonical build, verify identity and preserve installed-owner state.
- Runtime operation: follow [the agent guide](docs/agent-guide.md) and the relevant
  [runtime skill](skills/). Repository access does not authorize Telegram mutations.
