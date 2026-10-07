# Confirmed media and photo inspection

For reading, start with the [selected media/voice recipe](attachments.md#choose-the-path-before-processing):
inspect capabilities and one exact readable message before choosing OCR/STT or
inline pixels. `media_info`, photo rendering and ordinary message reads do not
start extraction or an AI upload. Captions remain original source evidence.

File-root permission is needed for standing owner source-file reads and an explicitly
requested new `destination_path`/`save_path`, not for job-private Telegram downloads
or inline photo rendering. Extraction/transcription accepts selected message IDs,
not arbitrary local media paths. Keep private copies under the existing owner data
root; grant only an exact output directory if saving is actually requested.

The owner grants file access while the daemon is stopped:

```text
teleloom profile file-root personal C:\owner-selected\media --enable
teleloom profile file-root personal C:\owner-selected\media --disable
```

For an explicit human instruction selecting the exact files and send target,
`media_operation_preview(..., owner_authorized=true)` authorizes only that immutable
send plan and its safely verified source files. It changes no file roots or chat
allowlists; bare uploads and reusable upload handles retain their configured-root
workflow. Default media previews require the configured file roots and send target.
Read permission applies separately to selected message photos, avatar owners and
reply/topic source messages. A file name, caption or photo does not grant permission.

Use `media_operation_preview` with one typed operation: `send_file`, `send_album`,
`send_voice`, `send_sticker`, `send_gif` or `upload_file`. Source paths must be
absolute, under a currently permitted file root or exactly owner-selected for this
authorized send, and free of links, junctions,
reparse points and traversal. A new optional download/save destination follows
the same root policy and cannot overwrite an existing file. Open file handles
are checked against the verified destination before private bytes are written.

Before returning a preview, the daemon copies selected bytes into a private
snapshot and records SHA-256, original name, MIME, size, source path, generation
and expiry. Each file permits at most 50 MB; an album has 2–10 compatible items
and at most 100 MB total. Private snapshots and retained downloads each have a
500 MB disk budget. Photos permit 10 MB and 16 million pixels. Captions permit
1024 UTF-16 units and remain plain text. Voice requires Ogg Opus; static stickers
require WebP, one 512-pixel edge and at most 512000 bytes. GIF sends accept actual
GIF/MPEG4 files or one exact result handle from Telegram's configured provider.

Show the entire immutable preview and obtain owner confirmation, then call the
existing `delivery_execute` with `plan_id`, `plan_hash` and `confirmed=true`.
Execute and worker revalidate current permissions, account, expiry, original
source and frozen hash. The worker validates bytes before marking the external
attempt, then passes those same bytes to the SDK upload. Albums reserve one
daily-message unit per item. An unknown outcome stays
visible for reconciliation and is never automatically repeated.

Scheduled user sends retain Telegram's schedule-queue IDs and explicit
`schedule_accepted`, `scheduled_for` and `delivery_confirmed=false` fields. A
completed local job records request acceptance; it does not establish delivery
at the future time. Incomplete native message receipts stay unknown without replay.

Bare `upload_file` is a confirmed external Telegram upload with an account target,
not a message send. Its receipt exposes an opaque upload handle for 15 minutes.
A later `send_file` preview can reuse that Telegram upload without reopening the
original source or uploading again. Handles bind account generation and profile;
they cannot be moved between profiles. GIF handles retain their provider result
and permit no replacement caption. Bot API cannot perform bare uploads, reuse
MTProto upload handles, schedule media or query the user-only inline GIF provider.

`media_info` preserves the original observed message and caption. `media_download`
accepts one exact message ID and a selected byte budget, saves privately for 24
hours by default, and returns original bytes/hash. `photos_list` lists avatars or
message-photo references. User-group avatar history uses Telegram chat-photo
search, not user-photo RPCs. Bot API exposes user profile photos, a group's current
avatar and message photos from saved updates; its result reports limited coverage.
An MTProto bot can open an exact group avatar service message through
`photo_open(avatar_message_id=...)` when historical enumeration is unavailable.

`photo_open` returns JSON metadata plus an inline MCP JPEG image. The original
hash/dimensions and rendered dimensions are separate; an optional `save_path`
stores original bytes. Rendering applies EXIF orientation, caps the long edge
at 2048 pixels and uses the first frame. `photo_sheet` composes at most twelve
labelled 256-pixel thumbnails. Missing or invalid images appear as per-photo
errors and make coverage incomplete. Pillow is included in the ordinary package.

Snapshots and handles expire after 15 minutes, retained private downloads after
24 hours. The worker removes expired copies automatically; `media_cleanup` does
the same for one explicit profile. Owner source files and explicit saved outputs
remain under owner control. Photo inspection and media delivery do not automatically
run OCR, transcription or AI. Explicit [transcription](reference.md#transcription)
and [attachment extraction](attachments.md) are separate selected workflows.

Public offline evidence uses authenticated HTTP MCP, subprocess stdio, owner CLI,
real temporary SQLite/journals and actual Telethon/aiogram method models, with only
the external Telegram boundary replaced. The ordinary installed-wheel smoke runs
inline photo opening, photo sheet composition and confirmed SDK-shaped sending
outside the checkout. This does not establish live Telegram or client acceptance.
