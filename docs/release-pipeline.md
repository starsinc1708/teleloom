# Release pipeline

Contract: [ADR 0005](adr/0005-build-provenance.md) and
[implementation standards](agents/implementation-standards.md).

Run `scripts/release.py` from a separate tooling environment with the project's
development dependencies. Never run `uv run` or `uv sync` against the selected
owner environment: that can restore an editable package. Commands use the target
interpreter directly with `-I -X utf8` and capture UTF-8. Preserve the client's
`pythonw.exe` launcher on Windows; probes use its sibling `python.exe`.

## Preparation and evidence

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\.scratch\release-tooling"
uv sync --frozen --all-extras --no-editable
$tooling = "$PWD\.scratch\release-tooling\Scripts\python.exe"
& $tooling -X utf8 scripts/release.py prepare --tag vX.Y.Z --commit FULL_SHA --output OUTSIDE_SOURCE --evidence evidence.json
```

Before a substantial release, run `uv run pytest` once on the final source unless
the owner cancels it. Run affected lint/type/skill/package checks during development,
not as a second release gate bundle. Release after a completed block of work.

The source must be clean at the pinned commit, including matching project/module/
lock versions and tracked `docs/releases/TAG.md`. Preparation clones that source,
builds once, verifies wheel/sdist versions, source stamp and exact hashes, then
collects schema/skill/build identity from a small isolated installed inventory.
It does not run pytest, Ruff, mypy, archive rebuilds or installed behavioral smoke.
It stamps
`release`/TAG/SHA and writes exactly two artifacts plus deterministic `SHA256SUMS`
under `OUTPUT/artifacts`; reports and the installed schema/skill inventory sit
outside that directory. An existing complete preparation is validated and reused;
partial or conflicting artifacts require a new output directory, never a silent
rebuild. Build, identity or installed-inventory failures block later phases.

Evidence records the one full-run result or the owner's explicit cancellation.
It requires a final `commit`, exact `tree` SHA and a nonempty evidence `reference`.
`result` is `passed`, `waived` or `not_required`; skipped tests never claim PASS.
Hosted CI and independent review receipts are not prerequisites unless the task
requires them. This operator attestation is not a live acceptance claim:

```json
{
  "tests": {"commit": "FINAL_SHA", "tree": "TREE_SHA", "result": "passed", "reference": "ACTUAL_FULL_RUN_RECEIPT"}
}
```

For a cancelled full run, use `result: "waived"` and reference the owner's decision.
For a small release, use `not_required` and reference the affected-behavior checks.
Preparation resolves the evidence commit in the source repository and requires
the pinned source/tree. Previously prepared reports with all eight original gates
and standards/spec/CI evidence remain supported and are not rewritten.

## Publication and independent audits

```text
python -X utf8 scripts/release.py publish --tag TAG --commit SHA --artifacts OUTPUT/artifacts --repo OWNER/REPO
python -X utf8 scripts/release.py verify-assets --tag TAG --commit SHA --artifacts OUTPUT/artifacts --repo OWNER/REPO
```

Publication is an explicit mutation using `git`/`gh`. It validates preparation,
source and the tracked notes before pushing a previously absent tag at SHA.
Existing tags and releases are inspected first. Matching complete releases are
downloaded and verified without replacing assets; conflicts or incomplete asset
sets fail. Rerunning after an uncertain publication inspects remote state before
any new mutation. An incomplete existing release requires explicit reconciliation
by the operator; there is no automatic deletion, replacement or `--clobber`.

`verify-assets` performs a read-only remote audit independently of publication.
Both commands download into a fresh directory, reject extra/missing/duplicate/
unsafe manifest entries and altered bytes, compare wheel/sdist stamps and require
byte identity with the prepared artifact. Identical downloaded bytes do not need
another build, archive reconstruction or installed behavioral smoke.
Only a passed report with `download_checks: "passed"` can select an installation.
The verified download stays available for that installation and later audits.

Each report pins source and artifact hashes and records `completed_stage`.
Subprocess output and exception details are suppressed in failure reports because
they can contain credentials or content. A failed gate returns a nonzero status.
Use stage identity to reproduce a failed check in the separate tooling environment;
do not claim a failed cleanup as successful verification.

## Selecting and preserving an owner

`--client` identifies a target descriptor, rather than guessing a home directory
or editing unrelated client settings. Example:

```json
{
  "data_dir": "C:/private/teleloom-data",
  "client_config": "C:/private/client.json",
  "server_name": "teleloom",
  "skills_dir": "C:/Users/owner/.agents/skills",
  "skills": ["teleloom-read", "teleloom-inbox"],
  "owner_pid": 12345
}
```

The client config uses `mcpServers[server_name]` with the selected Python command,
`args` ending in `-m teleloom mcp`, and its existing `env`. `owner_pid` is required
for an already running owner so its exit can be awaited; omit it for a stopped
owner. The config is read and hashed, never rewritten. Its environment, PATH,
credential references, Tesseract/model settings, launcher and unrelated settings
are retained. Credentials stay in the client's environment/OS backend and are
never serialized into stage reports or the baseline.

```text
python -X utf8 scripts/release.py upgrade --verified-release VERIFIED_REPORT --python TARGET_PYTHON --client TARGET_JSON --extras jev,pdf --previous-wheel OLD_WHEEL --previous-extras jev,pdf --state-dir PRIVATE_DIR
python -X utf8 scripts/release.py verify-owner --verified-release VERIFIED_REPORT --python TARGET_PYTHON --baseline PRIVATE_DIR/baseline.json
```

Supply the genuine previous wheel and previously chosen extras; the previous
wheel's identity must match the installed package. New extras must retain all
previous choices. Preflight blocks queued/running/paused jobs and pending/sending
receipts, editable or checkout imports, unexpected owners and unsafe skill paths.
No active operation is cancelled to make an upgrade proceed.

The private baseline records configuration/client/environment/permission hashes,
profile kinds/generations/capabilities and semantic historical job/receipt hashes.
Private recovery holds the old wheel, configuration bytes and selected skill
trees. No SQLite backup is used for recovery: normal polling may create newer
evidence, and historical unknown delivery states must remain exact.

After shutdown and awaited exit, the pipeline acquires the ownership lock, proves
the port closed, rechecks state, installs non-editably, and refreshes only selected
backed-up runtime skill trees. It releases the guard and starts the selected
launcher, then checks installed metadata, local doctor, MCP doctor and
`server_status`, release-specific schemas, bundled/installed skills, chosen engines
and preservation. Unrelated runtime skills are preserved. Developer skills are
maintained separately from `developer-skills/`; inspect their source/diff instead
of substituting a bundled runtime skill.

Successful invocation transfers the restarted process to the owner. Failure
awaits any child it started and reports a stopped or partially installed owner.
A saved baseline prevents a second upgrade even if final verification is pending.
Use `verify-owner` after explicit recovery/restart; it updates the saved journal
only after passing. Recovery information includes exact package/restart argument
arrays and selected backup/target skill paths. Restore only those package/skill
files after stopping the owner; retain the current database and credential backend.

Use `restore_skills_command` from the saved recovery report to restore exactly the
selected trees with verified backup hashes. It checks that the owner is stopped,
holds its ownership lock and refuses active jobs. Package and restart commands are
JSON argument arrays so Cyrillic paths and spaces require no shell interpolation.

## Dry runs and native client observations

Every phase supports `--dry-run`. Preparation identifies source/output; publication
identifies exact hashes and remote state; upgrade identifies the selected launcher,
extras, selected skills and preservation differences without stopping/installing.
Dry-run reports cannot authorize installation as verified downloads.

CLI/HTTP checks leave `native_client.status: "pending_reconnection"`. To record
native acceptance, reconnect the chosen client and observe its own `server_status`
and discovery. Pass a UTF-8 `--native-observation` file to `verify-owner`:

```json
{
  "client": "teleloom",
  "reconnected": true,
  "baseline_sha256": "CANONICAL_BASELINE_JSON_SHA256",
  "server_status": {"build": {"package_version": "X.Y.Z", "build_id": "TAG", "source_commit": "SHA", "build_type": "release"}},
  "schemas": {},
  "reference": "NATIVE_CLIENT_OBSERVATION_REFERENCE"
}
```

Use the actual discovery schemas and canonical baseline hash (UTF-8 JSON with
sorted keys, `ensure_ascii=False`, default JSON separators). The operator supplies
the native observation; HTTP success cannot manufacture it. Fake/offline tests
and CI do not establish live Telegram or native-client acceptance. Publication
and actual owner upgrade have their own phase reports; executed observations
are recorded in [acceptance](acceptance.md).

## Repository checks

Repository check selection, worker groups, timing receipts and the optional manual
CI compatibility checks are documented in [test-loop](test-loop.md). Use a separate tooling
environment when the checkout also contains the installed owner. Release
preparation reuses the actual source-bound test evidence and builds once; it
never launches a nested pytest suite.
