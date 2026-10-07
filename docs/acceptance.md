# Verification and limits

Results here distinguish automated checks, native client discovery and untested
behavior. [Implementation standards](agents/implementation-standards.md) define
completion checks; [release procedure](release-pipeline.md) defines artifact identity.

## First public release

Teleloom v0.5.0 starts from a fresh root history. The published release receipt
binds the final tested source/tree, full offline result, canonical artifact hashes,
secret scan and independent download audit. It is attached to
[the release](https://github.com/starsinc1708/teleloom/releases/tag/v0.5.0).
An editable checkout reports `build_type=dev`; release artifacts report their
exact release source. Preparation builds once and does not run a nested test suite.

## Native client discovery — 2026-10-07

These are historical observations against the pre-public **v0.4.0 installed owner**,
not live acceptance of the new v0.5.0 artifacts. Isolated client configurations
used the same installed stdio bridge and local owner.

| Client | Executed check | Observation |
|---|---|---|
| Codex | Native `server_status` / discovery | v0.4.0, 67 tools |
| OpenCode 2.0.24 | Native `mcp list`, `api mcp.list`, v2 config | Connected; native server log reported 67 tools |
| Hermes 0.21.5 | `hermes mcp test teleloom` | Connected, 67 tools |
| Pi 1.0.4 | `pi mcp list` | Connected, 67 tools |
| Claude Code 2.1.268 | `mcp add-json`, `mcp list` | Connected; catalog count not measured |

OpenCode starts asynchronously; an initial empty list was inconclusive until its
running private service connected. The test service was stopped afterward.
These probes made no model calls or Telegram reads/writes. Native model workflows,
automatic skill discovery and Claude Desktop GUI remain **NOT TESTED**; the owner
waived the Desktop GUI check. Connection success does not prove those behaviors.

## Native client installation correction — 2026-10-07

After PR #8, a Windows user check exposed two missed native contracts: equivalent
serialized path spellings failed the installer comparison, and Hermes cancelled
tool selection on EOF. Regression CLI checks now reproduce both; the affected
offline suite passed 32 cases on Windows / Python 3.12.14.

In isolated directories, native Hermes `mcp add` with one empty stdin response
saved an enabled server after discovering **one fake MCP tool**. Native OpenCode
`mcp add --global` saved the requested interpreter, arguments and data directory,
verified in its isolated JSON configuration. Hermes used a named profile under an
isolated `HERMES_HOME`; OpenCode's config path was checked before installation.
Initial probes against an unseeded isolated home timed out; the completed probes
used seeded temporary configurations. Neither check modified user configuration
or accessed Telegram. These are native CLI checks with a fake MCP endpoint, not
live acceptance of the owner's Teleloom connection; the new real-profile recheck
remains unperformed. OpenCode service connection after reload is also unverified.

## Automated contract coverage

Public CLI/MCP tests use fake Telegram/model APIs and real temporary SQLite,
queues, HTTP/stdio and isolated package processes. They exercise grants and profile
isolation, confirmation/hash binding, restart and unknown receipts, projection,
evidence lifecycles, paging/limits and release provenance/state preservation.
They do not measure real Telegram completeness or engine/model accuracy.

The [capacity study](research/reading-capacity-gate.md) is a bounded synthetic
single-owner sample, not end-to-end network throughput or a live SLO. Byte savings
are not token/context savings. Digest claim validation proves source linkage and
structural reuse rules; general semantic quality is **NOT MEASURED**.
