# Teleloom documentation

Teleloom is a Telegram toolkit with six portable agent skills, 67 MCP tools and a CLI.
Public version: **v0.5.0**. User guides describe implemented behavior; proposals
are identified in the [roadmap](roadmap.md).

## Start here

| Reader / task | Guide |
|---|---|
| New user | [English setup](getting-started.md) · [Установка на русском](install-ru.md) |
| Connect Codex, Claude, OpenCode, Hermes or Pi | [Client configuration and checks](clients.md) |
| Give an agent a useful task | [Example workflows and prompts](workflows.md) |
| Operate tools as an agent | [Agent usage guide](agent-guide.md) and the six [runtime skills](../skills/) |
| Look up a parameter or limitation | [MCP / CLI reference](reference.md) |
| Report a bug or contribute | [Contributing](../CONTRIBUTING.md) · [Security](../SECURITY.md) |

## Feature guides

[Response fields](response-fields.md) · [Attachments, PDF/OCR/transcription](attachments.md) ·
[Media](media.md) · [Account/group management](administration.md) ·
[Evaluation and measurement limits](evaluation.md) · [Docker and desktop bundles](deployment.md).

## Development and decisions

Coding agents start with [AGENTS.md](../AGENTS.md) and the
[implementation standards](agents/implementation-standards.md).
[Test selection / public CI](test-loop.md), [release procedure](release-pipeline.md),
[domain glossary](../GLOSSARY.md), [domain behavior](agents/domain.md) and [ADRs](adr/)
are the authoritative development references. [Specs](specs/) retain feature
contracts; the [capacity study](research/reading-capacity-gate.md) is a dated
synthetic measurement, not a live Telegram throughput claim.

[Acceptance](acceptance.md) separates automated checks, native discovery and
untested behavior. [Release notes](releases/v0.5.0.md) describe the first public
release. The private development history and its operational logs are not part
of this repository.
