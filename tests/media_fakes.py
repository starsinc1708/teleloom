"""Synthetic media bytes and SDK uploads shared by the public media contracts."""

from datetime import UTC, datetime

from telethon import functions, types

from tests.telegram_fakes import SDK


def encoded_image(format="PNG", size=(30, 20)):
    import io

    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", size, "blue").save(output, format)
    return output.getvalue()


def encoded_voice():
    header = (
        b"OpusHead" + bytes([1, 1]) + b"\x00\x00" + (48000).to_bytes(4, "little") + b"\x00\x00\x00"
    )

    def page(payload, granule, flags):
        return (
            b"OggS\x00"
            + bytes([flags])
            + granule.to_bytes(8, "little")
            + (1).to_bytes(4, "little")
            + b"\x00" * 8
            + bytes([1, len(payload)])
            + payload
        )

    return page(header, 0, 2) + page(b"\xf8\xff\xfe", 48000, 4)


class MediaSDK(SDK):
    def __init__(self):
        super().__init__()
        self.uploaded = []
        self.fail_send = False
        self.after_upload = None

    async def upload_file(self, file, *, file_size, file_name):
        content = file.read()
        assert len(content) == file_size
        self.uploaded.append((file_name, content))
        if self.after_upload:
            self.after_upload()
        return types.InputFile(123, 1, file_name, "synthetic-checksum")

    async def __call__(self, request, *args, **kwargs):
        self.calls.append(request)
        assert isinstance(request, functions.messages.SendMediaRequest)
        assert request.peer.channel_id == 100 and request.random_id
        assert isinstance(request.media, types.InputMediaUploadedDocument)
        assert request.message == "Exact caption" and request.entities == []
        assert request.media.force_file is True
        if self.fail_send:
            raise ConnectionError("PRIVATE_NETWORK_DETAIL")
        return types.UpdateShortSentMessage(id=20, pts=1, pts_count=1, date=datetime.now(UTC))
