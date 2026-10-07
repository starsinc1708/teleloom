# Broadcast execution

Default policy is 50 recipients/plan, one send every 5 seconds and 100 messages per
profile per UTC day. These are application limits, not guarantees against Telegram
restrictions. Configuration changes require a local owner action and daemon restart.

Pausing and cancelling apply before the next send; already delivered messages remain
delivered. Execution is idempotent by plan. A crash during send produces an unknown
item on recovery; it is not automatically replayed. Content edits require a new plan.
