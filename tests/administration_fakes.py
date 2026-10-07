"""External administration SDK fixture; function scope retains mutable state isolation."""

from datetime import UTC, datetime

import pytest
from telethon import errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession

GROUP = "-1000000000123"


class TelegramSDK:
    """Only Telegram is fake; the owner, adapter, MCP, SQLite and queue are real."""

    def __init__(self):
        self.calls = []
        self.now = datetime.now(UTC)
        self.session = MemorySession()
        self.session.auth_key = AuthKey(b"administration fake key".ljust(256, b"0"))
        self.me = types.User(id=7, first_name="Owner", username="owner", phone="SECRET_PHONE")
        self.user = types.User(id=8, first_name="Member", username="member", access_hash=998)
        self.participant_users = [self.user]
        self.group = types.Channel(
            id=123,
            title="Engineering",
            photo=types.ChatPhotoEmpty(),
            date=datetime.now(UTC),
            megagroup=True,
            access_hash=999,
        )
        self.full_group = types.ChannelFull(
            id=123,
            about="Team description",
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=0,
            chat_photo=types.PhotoEmpty(0),
            notify_settings=types.PeerNotifySettings(),
            bot_info=[],
            pts=0,
            participants_count=1,
            linked_chat_id=555,
        )

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return self.me

    async def send_read_acknowledge(self, *args, **kwargs):
        raise AssertionError("A bot inbox acknowledgment must never change Telegram read state")

    def add_event_handler(self, *args):
        pass

    async def get_input_entity(self, target):
        if target == int(GROUP):
            return types.InputPeerChannel(123, 999)
        if target in (8, 9):
            return types.InputPeerUser(target, 998)
        if target == -456:
            return types.InputPeerChat(456)
        if target == "me":
            return types.InputPeerSelf()
        raise ValueError(target)

    async def get_entity(self, target):
        if isinstance(target, (types.InputPeerChannel, types.InputPeerChat)):
            return self.group
        return self.user

    async def upload_file(self, file, *, file_name):
        self.uploaded = (file_name, file.read())
        return types.InputFile(id=20, parts=1, name=file_name, md5_checksum="")

    async def __call__(self, request):
        bytes(request)
        self.calls.append(request)
        if isinstance(
            request,
            (functions.channels.JoinChannelRequest, functions.messages.ImportChatInviteRequest),
        ) and getattr(self, "join_error", None):
            raise self.join_error
        if isinstance(request, functions.channels.ExportMessageLinkRequest):
            return types.ExportedMessageLink(
                link="https://t.me/c/123/88?thread=77", html="<private embed>"
            )
        if isinstance(request, functions.messages.GetPeerDialogsRequest):
            return types.messages.PeerDialogs(
                dialogs=[
                    types.Dialog(
                        peer=types.PeerChannel(123),
                        top_message=88,
                        read_inbox_max_id=80,
                        read_outbox_max_id=0,
                        unread_count=3,
                        unread_mentions_count=1,
                        unread_reactions_count=0,
                        unread_poll_votes_count=0,
                        notify_settings=types.PeerNotifySettings(),
                        folder_id=1,
                        unread_mark=True,
                    )
                ],
                messages=[
                    types.Message(
                        id=88,
                        peer_id=types.PeerChannel(123),
                        date=self.now,
                        message="Latest selected message",
                    )
                ],
                chats=[self.group],
                users=[],
                state=types.updates.State(pts=0, qts=0, date=self.now, seq=0, unread_count=3),
            )
        if (
            isinstance(request, functions.channels.EditBannedRequest)
            and not request.banned_rights.view_messages
            and getattr(self, "unban_error", None)
        ):
            raise self.unban_error
        if isinstance(request, functions.messages.CheckChatInviteRequest):
            return types.ChatInvite(
                title="Exact invitation", photo=types.PhotoEmpty(0), participants_count=1, color=0
            )
        if isinstance(request, functions.account.GetPrivacyRequest):
            return types.account.PrivacyRules(
                rules=[types.PrivacyValueDisallowAll()], chats=[], users=[]
            )
        if isinstance(request, functions.bots.GetBotCommandsRequest):
            return [types.BotCommand(command="old", description="Old menu")]
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            topic = types.ForumTopic(
                id=11,
                date=datetime.now(UTC),
                peer=types.PeerChannel(123),
                title="Topic",
                icon_color=0,
                top_message=11,
                read_inbox_max_id=11,
                read_outbox_max_id=0,
                unread_count=0,
                unread_mentions_count=0,
                unread_reactions_count=0,
                unread_poll_votes_count=0,
                from_id=types.PeerUser(8),
                notify_settings=types.PeerNotifySettings(),
                closed=False,
                hidden=False,
            )
            return types.messages.ForumTopics(
                count=1, topics=[topic], messages=[], chats=[], users=[], pts=1
            )
        if isinstance(request, functions.messages.CreateForumTopicRequest):
            return types.Updates(
                updates=[
                    types.UpdateNewChannelMessage(
                        message=types.MessageService(
                            id=12,
                            peer_id=types.PeerChannel(123),
                            date=datetime.now(UTC),
                            action=types.MessageActionTopicCreate(
                                title=request.title, icon_color=0
                            ),
                        ),
                        pts=1,
                        pts_count=1,
                    )
                ],
                users=[],
                chats=[],
                date=datetime.now(UTC),
                seq=1,
            )
        if isinstance(request, functions.messages.DeleteTopicHistoryRequest):
            return types.messages.AffectedHistory(pts=1, pts_count=1, offset=0)
        if isinstance(request, functions.messages.ExportChatInviteRequest):
            return types.ChatInviteExported(
                link="https://t.me/+fake-exact-invite", admin_id=7, date=datetime.now(UTC)
            )
        if isinstance(request, functions.account.UpdateProfileRequest):
            return types.User(id=7, first_name=request.first_name or "Owner")
        if isinstance(request, functions.photos.UploadProfilePhotoRequest):
            return types.photos.Photo(photo=types.PhotoEmpty(32), users=[])
        if isinstance(request, functions.photos.DeletePhotosRequest):
            return [31]
        if isinstance(
            request,
            (
                functions.messages.CreateChatRequest,
                functions.channels.CreateChannelRequest,
                functions.channels.InviteToChannelRequest,
            ),
        ):
            return types.messages.InvitedUsers(
                updates=types.Updates(
                    updates=[], users=[], chats=[self.group], date=datetime.now(UTC), seq=1
                ),
                missing_invitees=[],
            )
        if isinstance(
            request,
            (functions.channels.JoinChannelRequest, functions.messages.ImportChatInviteRequest),
        ):
            return types.Updates(
                updates=[], users=[], chats=[self.group], date=datetime.now(UTC), seq=1
            )
        if isinstance(
            request,
            (
                functions.channels.LeaveChannelRequest,
                functions.messages.DeleteChatUserRequest,
                functions.messages.EditChatAboutRequest,
                functions.channels.EditPhotoRequest,
                functions.channels.EditAdminRequest,
                functions.channels.EditBannedRequest,
                functions.messages.EditChatDefaultBannedRightsRequest,
                functions.channels.ToggleSlowModeRequest,
                functions.channels.ToggleForumRequest,
                functions.messages.EditForumTopicRequest,
                functions.account.SetPrivacyRequest,
                functions.bots.SetBotCommandsRequest,
                functions.photos.UpdateProfilePhotoRequest,
            ),
        ):
            return True
        if isinstance(request, functions.messages.GetFullChatRequest):
            return types.messages.ChatFull(
                full_chat=types.ChatFull(
                    id=456,
                    about="Basic group",
                    participants=types.ChatParticipants(chat_id=456, participants=[], version=1),
                    notify_settings=types.PeerNotifySettings(),
                ),
                chats=[],
                users=[],
            )
        if isinstance(request, functions.messages.AddChatUserRequest):
            if request.user_id.user_id == 9:
                raise errors.UserPrivacyRestrictedError(request)
            return types.messages.InvitedUsers(
                updates=types.Updates(
                    updates=[], users=[], chats=[], date=datetime.now(UTC), seq=1
                ),
                missing_invitees=[],
            )
        if isinstance(request, functions.channels.EditTitleRequest):
            self.group.title = request.title
            if getattr(self, "title_error", None):
                raise self.title_error
            return types.Updates(
                updates=[], users=[], chats=[self.group], date=datetime.now(UTC), seq=1
            )
        if isinstance(request, functions.channels.GetParticipantsRequest):
            users = self.participant_users[request.offset : request.offset + request.limit]
            return types.channels.ChannelParticipants(
                count=len(self.participant_users),
                participants=[
                    types.ChannelParticipant(user_id=x.id, date=datetime.now(UTC)) for x in users
                ],
                chats=[],
                users=users,
            )
        if isinstance(request, functions.channels.GetMessagesRequest):
            return types.messages.Messages(
                topics=[],
                messages=[
                    types.Message(
                        id=88,
                        peer_id=types.PeerChannel(123),
                        date=datetime.now(UTC),
                        message="arbitrary selected post",
                    )
                ],
                chats=[self.group],
                users=[self.user],
            )
        if isinstance(request, functions.photos.GetUserPhotosRequest):
            return types.photos.Photos(
                photos=[
                    types.Photo(
                        id=31,
                        access_hash=222,
                        file_reference=b"private",
                        date=self.now,
                        sizes=[],
                        dc_id=1,
                    )
                ],
                users=[],
            )
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return types.messages.ChatFull(full_chat=self.full_group, chats=[self.group], users=[])
        if isinstance(request, functions.channels.GetParticipantRequest):
            return types.channels.ChannelParticipant(
                participant=types.ChannelParticipantAdmin(
                    user_id=8,
                    promoted_by=7,
                    date=datetime.now(UTC),
                    admin_rights=types.ChatAdminRights(delete_messages=True),
                    rank="Editor",
                ),
                chats=[],
                users=[self.user],
            )
        if isinstance(request, functions.channels.GetAdminLogRequest):
            return types.channels.AdminLogResults(
                events=[
                    types.ChannelAdminLogEvent(
                        id=90,
                        date=datetime.now(UTC),
                        user_id=8,
                        action=types.ChannelAdminLogEventActionChangeTitle(
                            prev_value="Before", new_value="After"
                        ),
                    )
                ],
                chats=[self.group],
                users=[self.user],
            )
        if isinstance(request, functions.messages.GetCommonChatsRequest):
            chats = [
                types.Channel(
                    id=555,
                    title="Outside policy",
                    photo=types.ChatPhotoEmpty(),
                    date=datetime.now(UTC),
                ),
                self.group,
            ]
            return types.messages.Chats(
                chats=[x for x in chats if not request.max_id or x.id < request.max_id][
                    : request.limit
                ]
            )
        if isinstance(request, functions.users.GetFullUserRequest):
            self_request = isinstance(request.id, types.InputUserSelf)
            return types.users.UserFull(
                full_user=types.UserFull(
                    id=7 if self_request else 8,
                    settings=types.PeerSettings(),
                    notify_settings=types.PeerNotifySettings(),
                    common_chats_count=2,
                    about="User biography",
                    birthday=types.Birthday(day=12, month=8, year=1980),
                    personal_channel_id=555,
                    bot_info=types.BotInfo(
                        user_id=8,
                        description="Public bot biography",
                        commands=[types.BotCommand(command="help", description="Help")],
                    )
                    if self.user.bot
                    else None,
                ),
                chats=[],
                users=[self.user],
            )
        raise AssertionError(type(request))


@pytest.fixture
def sdk(monkeypatch):
    sdk = TelegramSDK()
    monkeypatch.setattr("teleloom.adapters.user_client", lambda *a, **kw: sdk)
    return sdk
