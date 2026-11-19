# ADR 0012: protected owner browser panel

Accepted scope: issue #20. The first browser page is a read-only inspector served
by the existing local owner. It must not become another connection owner, an MCP
client, or a place where long-lived credentials reach browser code.

## Boundary with MCP

The panel is mounted by the same daemon process on the same loopback listener as
MCP and reads the same `Settings`, `Runtime` and installed build artifact. Its
status contract is a projection of `server_status`: owner ID, build, workspace
fingerprint, profile count, local health and snapshot time. The panel request
path never calls a tool, opens an adapter, reads Telegram or invokes an AI
provider. Opening or refreshing the page therefore starts no external work.

MCP remains the only agent contract. `/mcp` still requires its bearer token,
DNS-rebinding protection, exposure and grants; the panel cookie is scoped to
`/ui`, is not accepted by `/mcp`, and cannot select a profile or tool. Panel
routes bypass only the bearer check inside the daemon; they do not change tool
registration, grants or the stdio bridge. A second owner is never started: the
CLI uses the existing `ensure_daemon` path, installed interpreter and selected
data directory, including `pythonw.exe` on Windows.

## Bootstrap and browser session

`teleloom ui` is an explicit owner action. After the normal health/start-up
path, it authenticates to the daemon with the MCP bearer token only in the
loopback CLI request and asks for a bootstrap. A bootstrap is random, single
use, expires after 60 seconds and lives only in the daemon process. The CLI
returns a URL whose fragment carries the bootstrap value; fragments are not
sent in HTTP requests, referrers or access logs. The page exchanges that
one-time value for an HttpOnly `SameSite=Strict`, `Path=/ui` cookie and
immediately clears the fragment. A passive visit to `/ui` has no bootstrap: it
only renders the sign-in instructions.

The cookie is not `Secure` because the in-scope listener is loopback HTTP; adding
a public listener is outside this contract and would require a separate review.

Sessions are random values held in daemon memory, with their own CSRF token and
an absolute expiry. Reuse of a consumed or expired bootstrap, a cookie from a
previous owner process, or a cookie from another browser session is rejected.
Logout consumes the session and expires the cookie. Restarting the owner
invalidates every browser session. Cookies ignore ports, so the panel cookie
uses a distinct, port-qualified name from the temporary login page and is
useless to any other local listener because only this process knows the session.
Server logs remain critical-only and never include paths, query strings, cookies
or tokens.

## Request policy

Every panel request requires the exact `Host` (`127.0.0.1:PORT`) and, when
present, the exact `Origin`. Browser POSTs require a same-origin `Origin`; the
bootstrap issue endpoint admits only the bearer-authenticated CLI. Logout
requires the session CSRF token in addition to the cookie. Responses use
`Cache-Control: no-store`, same-origin-only referrers (none on the bootstrap page), `nosniff`,
`frame-ancestors 'none'` and a restrictive CSP. Dynamic values are escaped as
text; the page uses semantic labels, visible focus and accessible
loading/error/empty states. Long-lived MCP tokens, Telegram credentials, private
file paths and keyring values never enter HTML, cookies, URLs, storage or logs.

## Extension points

`status_snapshot(settings, runtime)` is the single read-only status projection
used by the page and `server_status`; #21 can extend it with profile, grant and
tool detail without adding a second source of truth. `Panel` owns authenticated
GET routes and is the place for #22 read-only job/receipt views using existing
stores. #32 reuses the same bootstrap over an SSH local-forward and can show
`workspace_id` to distinguish the remote owner; it must keep the listener
loopback-only, preserve the forwarded host/port in `Host`, and must not add a
public origin.
