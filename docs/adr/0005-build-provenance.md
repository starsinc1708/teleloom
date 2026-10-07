# ADR 0005: artifact-owned build provenance and safe diagnostics

Accepted scope: issue #31. A release wheel and a later local build can share the
same package version. Version alone therefore cannot prove which code a client
or a long-running owner uses. Runtime Git inspection also depends on an unrelated
working directory and requires tooling that end users may not have.

The artifact carries one compact contract, exposed under `build`:

| Field | Meaning |
| --- | --- |
| `package_version` | Installed distribution version |
| `build_id` | Build-time identifier, normally a UUID |
| `source_commit` | Exact source commit when available at build time, otherwise `unknown` |
| `build_type` | `release`, `local`, `dev` or `unknown` |

The build hook stamps the package at build time. Ordinary wheel/sdist builds are
`local`; editable installs are `dev`. `TELELOOM_BUILD_TYPE` can explicitly select
`release`, `local` or `dev`; `TELELOOM_BUILD_ID` can supply a safe identifier. New
release stamping requires a clean Git source tree with a known commit. A local
build ID distinguishes artifacts without pretending that a commit identifies
uncommitted changes. Building from a source archive preserves its stamped
metadata; an archive without provenance reports unknown source commit.

Runtime reads and validates the installed artifact's metadata at process startup,
then retains that identity for its lifetime. Replacing a wheel's files cannot
make an already running owner claim the replacement build. It neither reads
Git from the current directory nor accepts runtime environment replacements.
Missing or invalid provenance stays explicitly `unknown`. The existing status
version field remains compatible. `server_status` reports the same contract as
the installed CLI's `doctor`; MCP exposes no credentials or private paths.

`doctor --mode local` is the default and checks the invoking installation without
starting a daemon. `doctor --mode mcp` inspects an already running owner and its
discovered tool schemas, allowing local versus owner metadata to be compared.
It does not start one. The existing `--live` flag remains an alias for this MCP
connectivity check and preserves its legacy `live` profile-snapshot envelope.
Diagnostics use safe status/discovery and local engine
availability checks, with concrete recovery actions; neither default mode reads
Telegram or makes a paid AI call. T03/#62 adds an explicit profile/chat opt-in to
`server_status` and doctor for one bounded user history request. The local default
and legacy `--live` alone retain their original no-read behavior. The probe checks
the normal reader's exposure and current ACL, uses an exact saved peer without
dialog enumeration, and reports logical attempts separately from unknown SDK RPC
counts. See [the diagnostic contract](../reference.md).

Routine release verification checks wheel/sdist version, source stamp and hashes
from one build, then compares downloaded bytes with that artifact. Preparation
collects schema/skill identity from a small isolated installed inventory; it does
not rerun pytest or behavioral smoke. The full archive checker (including rebuild
without Git, unknown-source and version-mismatch cases) and installed CLI/stdio
smoke remain targeted checks when build/provenance/packaging behavior changes.
Run each relevant checker once, not again on byte-identical downloads. These
checks do not establish owner upgrades, live Telegram or client reconnection;
owner state preservation and actual native discovery have their own observations.
