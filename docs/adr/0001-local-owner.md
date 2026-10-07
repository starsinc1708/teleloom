# ADR 0001: one local connection owner

The daemon owns all Telegram runtime connections. MCP stdio bridges use its
authenticated loopback HTTP endpoint. An OS-released file lock protects ownership
across processes; authentication acquires that lock too. Login requires stopping an
active daemon, preventing concurrent use of a session. QR login uses a temporary
loopback page or terminal, never MCP credential arguments.

Stdio bridges keep tool discovery metadata, but open a fresh authenticated HTTP
session for each call. The next call can start a stopped daemon; an interrupted
call is never automatically replayed. Reads have a 30-second default deadline;
the bridge allows 15 additional seconds for local connection setup and cleanup.

SQLite is the durable source of jobs, plans, and index checkpoints. An in-flight
send becomes unknown on restart rather than being replayed.
