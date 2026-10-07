# ADR 0002: dialogue confirmation

The user chose confirmation inside their agent conversation. Preview creates an
immutable hash-bound plan with a 15-minute start deadline. Execution requires the
matching plan hash and an explicit confirmation flag. The server cannot prove that
the flag came from a human; this is a documented client trust boundary, not a
cryptographic approval. No generic Telegram method passthrough bypasses delivery.

Issue #40 extends the same plan and durable journal to typed message operations.
Legacy send permission authorizes sending; it does not silently authorize edits,
deletions, reactions, pins, read acknowledgments, drafts or chat state changes.
Those actions require the owner's separate mutation scope. The full operation,
resolved targets and reviewed source versions are hash-bound. Classic formatting
is parsed at preview and its exact text/entities are frozen; rich input remains
explicitly server-parsed. The worker rechecks scope and sources before recording
an in-flight action, and unknown outcomes remain unreplayable. Confirmed scheduled
acceptance is a receipt for stored scheduling, not proof of eventual delivery.
