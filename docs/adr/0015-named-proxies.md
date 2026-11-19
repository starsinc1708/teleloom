# ADR 0015: named proxy endpoints and explicit profile routes

Status: **implemented** (#28), owner-reviewed Apply on top of ADR 0013. Scope:
the named proxy catalogue, one explicit assignment per profile, migration of the
existing per-profile proxy secret, and one bounded reachability check. No
failover, health scheduler, MTProxy or automatic routing is included.

## Contract

- `Settings.proxies: dict[str, ProxyEndpoint]` in `config.json`. An endpoint is
  `protocol` (`socks5`, `socks4` or `http` — exactly the schemes the Telethon and
  Bot API adapters already accept), `host` (bare hostname or IPv4; a scheme or
  path is rejected) and `port` (1–65535). Names match `[a-z][a-z0-9_-]{0,63}`.
- `Profile.proxy: str | None` is the assignment. Absent means "no assignment";
  the legacy per-profile secret `proxy` continues to be used verbatim, so every
  existing profile keeps its current route without migration.
- Credentials never enter `config.json`. A named endpoint's username/password
  are one JSON value in the existing `Secrets` boundary under
  `("proxy", name)`; `Secrets` keeps OS-keyring/env precedence. UI, MCP, export,
  results, errors and logs see only protocol/host/port, the assignment name and
  `stored`/`absent`. Apply takes credentials in the POST body only; review URLs
  and review pages never carry or re-render them.
- Apply reuses the ADR 0013 stop → atomic write → start → verify transition.
  The owner reports `proxy: {"name": ...}` per profile in `server_status`, and
  verification compares both the file and that live report. A stale revision is
  refused with 409; invalid protocol/host/port/name or an unknown assignment is
  refused with 422 before anything is written or stopped.

## No direct fallback

`adapters.proxy_url` resolves the route once per connection:

- no assignment → the legacy secret, unchanged;
- assignment → the named endpoint plus its stored credential;
- assignment whose endpoint is missing → `TeleloomError("proxy_unavailable")`.

There is no branch that returns `None` after an assignment was requested, so a
missing or unusable proxy cannot silently connect directly, switch backend or
retry an uncertain send. FloodWait and account restrictions are interpreted by
the existing error mapping, never as a reason to change the route.

## Migration

The page offers an explicit `migrate` action: for each profile that has a legacy
proxy secret and no assignment, it creates `legacy_<profile>` from the parsed
URI, copies the decoded username/password into the named secret and assigns the
profile. The legacy secret is left in place. Migration is reviewed as a diff and
applied like any other change; an unparsable legacy URI blocks it entirely.

## Route check

`GET /control/proxies?check=<name>` is a separate, explicitly requested action,
never part of opening the page. It opens one TCP connection to the configured
host/port with a clamped 0.05–10 s deadline, never sends credentials, performs no
Telegram RPC and reports `reachable`/`timeout`/`unavailable` with
`direct_fallback: false`. It proves the proxy listener answers, not that the
upstream route works; ordered fallback and health checks are separate work.

## Consequences

- Deleting an assigned endpoint is not offered; replace the assignment first.
  Editing an endpoint lists every affected profile in the diff.
- Known limit: a probe is a listener check, and only one assignment per profile
  exists. Multi-endpoint failover needs its own ADR.
