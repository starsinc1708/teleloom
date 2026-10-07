---
name: teleloom-connect
description: Connect a named Telegram user account or bot to teleloom, or diagnose an unavailable profile. Use QR login through a local browser or terminal.
---

# Connect Telegram

Call `server_status` and `profiles_list` first. An existing authorized profile needs
no new login. Local/MCP status and a cached connection do not establish live Telegram
health. For an unavailable profile, follow the [diagnostic and recovery recipe](references/connection.md)
before requesting authentication. Have the owner run `teleloom auth user --profile NAME --ui browser` or
`--ui terminal`; stop an active daemon before authentication. The owner scans the QR
in Telegram's Devices settings and enters any cloud password directly on the local
page or hidden terminal prompt. For a bot use `teleloom auth bot --profile NAME`.
`profiles_list` distinguishes live user folders/topics/comments/pins from saved
bot updates. `attachment_capabilities` reports local PDF/OCR/transcription engines
and setup instructions. An absent engine is not an authentication failure; never
download models automatically. Reconnect the MCP client after installing an update
that adds tools, because the stdio bridge caches discovery metadata.

For an event watch, verify the opted-in readable chats, polling and exposed tools,
then read the [host capability and pull recipe](../teleloom-inbox/references/events.md).
A connected profile establishes local pull, not a host wakeup or notification.

Keep credentials out of conversation and MCP arguments. After the owner completes
login, reconnect MCP and verify the profile identity and capabilities. Complete when
the intended identity is connected, or report the exact remaining human action.

Read [connection reference](references/connection.md) for fallback and polling.
