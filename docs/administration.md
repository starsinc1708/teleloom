# Group and account management

`administration_read(profile_id, operation)` reads exact `chat`, `participants`
(`all`, `admins`, `banned`), `member`, `audit` and `common_chats` metadata.
`chat` includes full description/count/avatar plus exact user dialog unread,
archive and latest-message state; use `include_dialog=false` for metadata only.
Private peers use full user metadata. `message_link` calls Telegram's real
`channels.exportMessageLink` with `thread`/`grouped` flags for a selected message;
it is user-only. HTML embeds are omitted.
IDs are canonical strings. Participant evidence belongs to the selected group;
it authorizes neither private user reads nor send, broadcast or AI. Pagination
reports bounded live pages; cursors expire after 15 minutes and bind generation,
read policy, query and limit. Linked/common/personal chats outside the read policy
are omitted. Results contain no access hashes, phone numbers or SDK dictionaries.

`account_read(profile_id, operation)` reads `me`, `user`, `bot_info`, `status`,
`photos`, `privacy` or `bot_commands`. Names, biographies, commands and audit
changes remain untrusted data. Privacy inspection requires explicit account
management permission. Avatar history exposes safe IDs and dimensions; image
inspection/download uses the shared media workflow and bounded private files.

`administration_preview` and `account_preview` accept strict discriminated
`group_*` and `account_*` operations: creation, join/leave, invitations,
title/about/photo, admin rights, ban/unban/remove, default permissions, slow mode,
forum/topic CRUD, exported invites, profile/privacy/photos and bot command menus.
Owners enable `groups` or `account` through `teleloom profile management PROFILE
SCOPE --enable`; defaults deny both. Existing targets require read permission
for the reviewed metadata. Preview freezes generation, exact targets, before-state
and expiry. Execute with the existing `delivery_execute`, matching plan ID/hash
and explicit confirmation, then inspect `jobs_status`. A changed before-state
requires a fresh preview. Unknown outcomes require reconciliation without replay.
Bot API ban/remove previews explicitly disclose deletion of the member's chat
messages; topic deletion previews disclose deletion of the topic's messages.

Account/create/join targets use an explicit account descriptor, with no artificial
chat recipient. Multi-step eject/unban, bot admin-rank/topic changes and basic
group invitations persist individual receipts. Exporting an invite is a confirmed
write. Photo plans use the shared root-confined immutable file snapshot; workers
upload copied bytes from the reviewed snapshot.
Invite imports accept an exact hash or a canonical `https://t.me/+HASH`,
`https://t.me/joinchat/HASH` or `tg://join?invite=HASH` link.
Join/import receipts distinguish `joined`, `already_joined` and `approval_pending`;
an accepted approval request does not claim membership and is not replayed.

Bots default to aiogram's Bot API. Explicit owner provisioning uses
`teleloom auth bot --profile NAME --backend mtproto --api-id ID`, with private bot
token/API hash inputs and OS credential storage. The same owner/session lease
governs this backend. It supports exact `messages_get(message_ids=...)`, full
participants/user metadata and avatar history. Bot inbox acknowledgment consumes
only the reviewed local update watermark; it never sends Telegram read-ack.
Edits collected after the snapshot remain pending across daemon restart.
Bot API gaps return
`backend_required`, distinct from genuine `platform_restriction`. Telegram history,
common chats, audit, group creation/join/invitation, slow mode/forum enabling and
account profile/privacy are user-only. Commands belong to the exact authenticated
bot. Basic groups have an admin flag rather than granular channel rights/ranks.

Primary method restrictions:
[participants](https://core.telegram.org/method/channels.getParticipants),
[explicit messages](https://core.telegram.org/method/channels.getMessages),
[avatars](https://core.telegram.org/method/photos.getUserPhotos),
[history](https://core.telegram.org/method/messages.getHistory),
[profile](https://core.telegram.org/method/account.updateProfile),
[invitations](https://core.telegram.org/method/channels.inviteToChannel),
[audit](https://core.telegram.org/method/channels.getAdminLog).

Offline evidence uses real HTTP MCP, owner/adapter paths and temporary SQLite;
only external APIs/credentials/clocks are fake. These checks do not establish live
Telegram or client acceptance.
