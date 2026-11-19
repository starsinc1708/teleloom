# Open your VPS owner panel through SSH

Scope: issue #32, a single owner whose VPS already has a provisioned Teleloom
installation, data directory and Telegram credentials. Provisioning, migration
and backup/restore remain separate M5 tasks; this page does not create a second
owner, a grant or browser credentials.

The panel is the existing `teleloom ui` page from
[ADR 0012](adr/0012-browser-panel-session.md). SSH only carries loopback HTTP:
the remote owner keeps its single loopback listener and no public listener is
added. The browser session is authorised by the remote owner with a one-time
bootstrap, exactly as on a local install.

## 1. Choose the remote workspace explicitly

Resolve the installed interpreter and data directory before connecting. Use the
same OS user, wheel and `TELELOOM_DATA_DIR` as the remote owner; a different
interpreter or directory starts a second owner with different state.

```text
Host vps-owner
  HostName 203.0.113.10
  User teleloom
  IdentityFile ~/.ssh/id_ed25519_teleloom
```

Keep native host-key verification. Do not add `StrictHostKeyChecking=no`,
`UserKnownHostsFile=/dev/null` or `UpdateHostKeys=no`; accept a new key once
through `ssh vps-owner` after comparing its fingerprint with the provider console.
Hosts that need a jump use `ProxyJump` in the same config file.

## 2. Forward one loopback port with the owner's exact port

The panel accepts only its exact `Host` (`127.0.0.1:PORT`) and `Origin`. The
local target port must therefore equal the remote owner port, so that the
browser and the daemon agree on the same origin. Pick the remote port from its
`config.json` (`port`, default `8765`).

<!-- test:ssh-forward -->
```bash
ssh -o ExitOnForwardFailure=yes -N \
  -L 127.0.0.1:8765:127.0.0.1:8765 vps-owner
```

`ExitOnForwardFailure=yes` makes a busy or refused local port a hard failure
instead of a silently degraded session. Both forward addresses are explicit
loopback: the forward is private to this machine and no `-R` or wildcard bind is
used. Keep this terminal open; it owns the tunnel.

If the local port is busy, stop the process that holds it (`ss -ltnp` on Linux,
`lsof -nP -iTCP:8765 -sTCP:LISTEN` on macOS) and retry. Do not forward to a
different local port and do not disable origin checks: a differing local port
produces `403 forbidden_origin` and cannot be made to work.

## 3. Issue the bootstrap on the remote owner and open it locally

Run `ui --no-open` through the same SSH host so the remote interpreter and data
directory issue the URL. The one-time fragment is printed on the SSH channel's
stdout; it is not an SSH argument and must not be copied into a shared ticket,
chat or shell history.

<!-- test:ssh-bootstrap -->
```bash
(
  umask 077
  bootstrap=$(mktemp "${TMPDIR:-/tmp}/teleloom-ui.XXXXXXXX") || exit
  trap 'rm -f -- "$bootstrap"' 0
  trap 'exit 1' HUP INT TERM
  ssh vps-owner 'cd /srv/teleloom && TELELOOM_DATA_DIR=/srv/teleloom/data \
    exec .venv/bin/teleloom ui --no-open' > "$bootstrap" || exit $?
  python3 -c 'import json,sys,webbrowser; sys.exit(not webbrowser.open(json.load(open(sys.argv[1]))["url"]))' \
    "$bootstrap"
)
```

The browser exchanges the fragment for an HttpOnly `Path=/ui` session cookie and
clears the fragment before the next navigation. The URL never contains the MCP
bearer token. The snippet opens the browser only after SSH exits successfully
and removes the private temporary file when it exits. If SSH fails or its
stdout contains a banner instead of clean JSON, fix the underlying issue before
requesting a new bootstrap.

## 4. Verify that this is the remote owner

The status page must show the remote `Owner ID`, `Workspace`, `Version`,
`Build ID`, `Source commit`, profile count and snapshot time. Compare the
workspace fingerprint with the remote `teleloom doctor --mode mcp` output and
the build with the installed wheel. MCP doctor can report the live owner only
when the owner exposes `server_status`. If it is hidden, compare the
authenticated panel's Workspace and Build ID with the expected values from the
same installed interpreter and data directory on the VPS:

```bash
ssh vps-owner 'cd /srv/teleloom && TELELOOM_DATA_DIR=/srv/teleloom/data .venv/bin/python -c "
from teleloom.build_info import build_info
from teleloom.config import Settings
from teleloom.runtime import fingerprint
print(fingerprint(str(Settings.load().data_dir.resolve())))
print(build_info()[\"build_id\"])
"'
```

This comparison leaves tool exposure unchanged. A mismatch means the tunnel or
bootstrap targeted a different workspace or installation.

Open and refresh the page, then sign out. Refreshing returns the same owner
snapshot and starts no Telegram or AI work. Sign-out revokes only that browser
session.

## 5. Failure triage

| Symptom | Meaning | Action |
|---|---|---|
| SSH exits with `bind: Address already in use`, or `ExitOnForwardFailure` | local port/transport failure; nothing was bootstrapped | free the port or stop the stale tunnel; the remote owner is untouched |
| SSH connection refused, timeout, or host-key mismatch | SSH transport failure | fix reachability or compare the host key; never disable verification |
| page shows `Panel locked` and no cookie | no browser session, expired session, or the owner restarted | issue a new bootstrap with `teleloom ui --no-open` |
| `/ui/session` returns `invalid_bootstrap` | bootstrap expired (60 s), was already exchanged, or belongs to another owner process | issue a new one; reuse is rejected by design |
| `403 forbidden_origin` | local port, `Host` or `Origin` differ from the remote owner | forward the remote port unchanged |
| bootstrap request fails, or `doctor` cannot reach the owner | remote owner failure | inspect the remote process, data directory and logs; start the remote owner with its installed interpreter |
| tunnel closes after the page loaded | transport ended; jobs and receipts stay as they were | reconnect; do not assume a delivery failed and do not replay it |

Closing the tunnel never deletes jobs, never turns an interrupted delivery into
a failed one, and never replays a send. The page is read-only; decisions about
sends remain in MCP previews and owner confirmation.

## 6. Pending live acceptance

The automated checks use a disposable loopback forwarder and a fake SSH
boundary; they verify the HTTP/session contract, port identity and argument
safety, not cloud-network behaviour. Run this page once against the chosen VPS
and record the owner ID, build ID, forwarded port, triage result and the date
before calling the SSH path accepted. Until then, live VPS acceptance is
pending.
