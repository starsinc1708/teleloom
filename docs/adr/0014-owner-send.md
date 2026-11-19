# ADR 0014: one owner-only send through exact preview and Confirm

Status: **implemented, no live send**. Scope: issue #31, one plain-text message
to one numeric chat from the existing local `teleloom control` process. Media,
broadcast, schedules, edits, deletes, formatting and custom emoji are not part of
this workflow.

## Boundary

`/control/send` lives in the control process from ADR 0013, not in the daemon's
`/ui` panel. It serves four GET pages and two POST steps over the reserved
loopback listener:

- `/control/send` — compose form; renders profiles and typed fields only. It
  opens no Telegram connection and mints no plan.
- `POST /control/send/preview` — validates one exact decimal chat ID
  (`control_policy.chat_id`), then calls the owner's registered
  `delivery_preview` with `owner_authorized=true` and the exact text. The owner
  resolves the target from Telegram and writes the immutable plan to its own
  `workspace.sqlite`; the page redirects to the plan ID.
- `/control/send/plan/{id}` — reads that plan row directly from the owner's
  workspace store (same OS owner, same data directory) and renders sender
  identity, resolved numeric chat and title, full text, side effects, plan ID,
  exact hash and expiry.
- `POST /control/send/confirm` — the separate human step. It requires the
  control session cookie, same-origin `Origin` and that session's CSRF token,
  then calls `delivery_execute` with exactly the stored plan ID and hash.
- `/control/send/job/{id}` — the durable receipt, projected from the owner's
  `jobs_status`: state, durable job ID, delivery receipts, and an explicit
  unknown-outcome warning.

The MCP bearer is never accepted as browser authority. Every `/control/send`
route requires the control bootstrap session; an `Authorization: Bearer` header
is ignored and returns the signed-out page. The bearer is read from the OS store
only inside the outbound MCP hop for one call (`daemon.session`, the same helper
`Lifecycle.observed` uses), and never appears in HTML, cookies, URLs, logs or a
request field. The browser cannot choose a plan, hash, grant or tool.

## Confirm invariants

- The submitted `plan_hash` is compared in constant time with the durable plan
  row and must match exactly; a changed byte is refused without calling the
  owner.
- The durable plan row is the idempotency record. Two clicks, a reload, a back
  navigation or a lost HTTP answer all reopen the same `job_id`; `execute`
  returns the existing job before any expiry or send path.
- Expiry is enforced by the owner at execute time, so a plan that expires
  between review and Confirm is refused with no job and no Telegram write.
  An expired plan renders no Confirm button.
- Read scope, exposure, account identity and message rights remain the owner's
  existing checks; the control page adds no grant and does not widen
  `exposed_tools`. If `delivery_preview`, `delivery_execute` or `jobs_status` is
  not registered, the form is disabled and says so.
- A lost or unknown execute answer is reconciled against the plan's `job_id`.
  If the job exists, the page follows it; a job with an `unknown` delivery is
  shown as unknown and is never resent or replayed. No automatic retry exists.
- Form data is escaped; the page renders no Telegram content other than the
  resolved title, and uses the ADR 0012 CSRF/CSP/Host/Origin contract.

## Deliberate limits

The control process does not start the owner (`auto_start=False`); a stopped
owner is a 503 and nothing is sent. It reads the plan directly from SQLite
rather than adding a preview-read MCP tool, because both processes already run
as the same owner and the plan row is the same immutable record the owner uses.
Live sending was not exercised in the test suite: all delivery tests use the
fake Telegram adapter and temporary state.

## Evidence

`tests/test_control_send.py` covers the default-deny browser boundary (bearer,
foreign Origin, wrong CSRF, named chat), `preview` creating no job or send,
exact hash refusal, double-click/reload returning the same job, durable job and
receipt rendering, lost execute response, expired plan, unknown outcome with no
resend, hidden exposure, and one real owner over loopback MCP with fake
Telegram. The focused suite plus the existing control/panel tests pass; ruff and
mypy pass on the touched modules.
