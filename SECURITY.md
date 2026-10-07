# Teleloom security and trust boundaries

teleloom is a single-owner local tool, not a multi-tenant service. All MCP clients
using the owner's token can read accessible chats and submit configured delivery
plans or request authorization for an exact owner-instructed send. The dialogue
confirmation and `owner_authorized` flags are trusted client input, not proof of a
human. Skills do not change persistent grants; Telegram content cannot authorize actions.

`owner_authorized=true` on a delivery preview records permission for that immutable
send plan only, including exact recipients and selected local file bytes. The
client must obtain the instruction directly from the human owner. It changes no
allowlists, file roots, read policy or tool exposure. Other mutations and bare
uploads retain their owner-configured permissions. Execution still requires the
matching plan hash and explicit confirmation; account/read/file checks and delivery
budgets apply during execution and recovery. A cancelled job cannot continue unsent work.

The daemon binds to 127.0.0.1, checks Host/Origin and requires bearer authentication
for MCP, health and owner shutdown. Stdio stdout contains only MCP messages. Tokens
are read by the bridge from OS credentials or environment, never tool outputs.
Do not expose the port through a public reverse proxy.

QR auth uses a short-lived loopback HTML page with an unguessable entry path and
HttpOnly SameSite cookie. Password POSTs require same Origin and a CSRF header.
The QR token is an authentication capability: scan it yourself, do not share it.
No external QR generator/CDN is used. Cloud passwords are not persisted; Python
cannot guarantee physical zeroization of immutable strings in RAM. OS malware or
another process running as the same owner is outside this protection model.

New private data directories use restrictive POSIX permissions or Windows ACLs.
An existing TELELOOM_DATA_DIR retains its existing permissions: choose a private,
non-shared directory. Credentials use OS keyring/environment, not config.json.
Index data, delivery plans/receipts, exports and Jev caches can contain personal
messages. There is no automatic expiration of the archive or full-disk encryption
provided by teleloom. Cache pruning deletes indexed messages and Jev caches, not audit
plans or exported files. Remove those separately when no longer needed.

Delivery is reserved durably before the external call. Unknown outcomes are never
automatically retried. Telegram RPC rate-limit responses postpone jobs; account
restrictions stop them. Limits do not evade or replace platform restrictions.
Allowlisting a recipient is an owner's operational decision, not proof of recipient
consent. No participant harvesting, automatic allowlist expansion or account rotation.

Jev receives selected message text only after per-chat opt-in. The current agent
already receives data requested through MCP; choosing a local daemon does not make
an external agent provider local. Owners are responsible for applicable content,
privacy and Telegram terms. No model-training pipeline is included.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/starsinc1708/teleloom/security/advisories/new).
Share a minimal reproduction and affected version through that private channel.
Public issues are for sanitized functional bugs; keep credentials and session
material out of both examples and logs. Rotate a leaked bot/API/MCP token and
revoke affected Telegram sessions from Devices.

The first public version is v0.5.0. Security fixes target the latest public release;
older private versions are outside public support. This is a single-owner local
trust model, not a multi-tenant or remotely exposed service.
