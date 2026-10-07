# Changelog

## 0.5.0 — 2026-10-07

First public release as **Teleloom**, with a clean Git history. Teleloom is a
Telegram toolkit comprising an MCP server, six portable agent skills and a CLI.
The package, module, CLI, MCP server, skills and local state/credential namespaces
consistently use `teleloom` / `TELELOOM_*`.

- 67 MCP tools: scoped accounts/bots, history/search/replies/topics, inbox,
  collection jobs, frozen evidence, typed counters, excerpts and exports.
- Verifiable digest/revision workflows, reusable field selection and compact
  output with original continuation and explicit coverage limitations.
- Confirmed message/media/contact/folder/account/group operations, durable
  receipts and safe handling of unknown delivery outcomes.
- Explicit OpenCode 2 and v1 generator targets; native Pi configuration and skill
  installation, alongside Codex, Claude and Hermes.
- Clear reader limits and exclusive cursor/reference descriptions; bundled IANA
  timezone data for calendar/DST calculations in the digest workflow.
- Public setup, user prompts, agent instructions, contribution/security guides,
  topics/badges and isolated GitHub-hosted CI.

Runtime capability development predates this first public release; previous
private releases and operational history are not republished. See
[acceptance](docs/acceptance.md) for actual observations and testing boundaries.
