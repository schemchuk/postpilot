import itertools
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
import test_store

from postpilot.ai.client import AIError
from postpilot.ai.humanizer import HumanizerError
from postpilot.core.models import Item
from postpilot.core.publisher import Publisher
from postpilot.core.states import ItemState
from postpilot.notify.approval import ApprovalHandler
from postpilot.sources.own_video import OwnVideoInbox
from postpilot.sources.telegram import TelegramInbox

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn
repo = test_store.repo
ids = itertools.count(1)


@pytest.fixture
def bot(repo):
    api, model = Mock(), Mock()
    api.send_message.side_effect = lambda *args, **kwargs: next(ids)
    handler = ApprovalHandler(
        repo,
        api,
        12,
        inbox=TelegramInbox(repo, api, model),
        own_video=OwnVideoInbox(repo, api, model),
    )

    def event(update_id=None, action=None, **body):
        header = {"from": {"id": 12}, "chat": {"id": 12}, "message_id": next(ids)}
        update = {"update_id": update_id or next(ids)}
        if action:
            update["callback_query"] = {
                "from": header["from"],
                "message": header,
                "id": "callback",
                "data": action,
            }
        else:
            update["message"] = {**header, **body}
        handler.handle_update(update)

    return api, model, event


@pytest.fixture
def media():
    return {
        "file_id": "file",
        "file_unique_id": str(uuid4()),
        "duration": 91,
        "file_size": 20_000_000,
    }


def video_row(conn, media):
    with conn.transaction():
        return conn.execute(
            "SELECT id, prompt_message_id, interview, status, item_id FROM own_videos WHERE file_unique_id = %s",
            (media["file_unique_id"],),
        ).fetchone()


def test_rejected_size_duplicate_and_skip(bot, conn, media):
    api, model, event = bot
    event(video={**media, "file_size": 20_000_001}, caption="/work")
    assert video_row(conn, media) is None
    assert api.send_message.call_args.args[1].startswith("Video over 20 MB:")
    event(text="/work")
    assert (
        api.send_message.call_args.args[1]
        == "Send a video with caption: /work [ai] [interview text]"
    )
    event(video=media, caption="/work", update_id=500)
    video_id, prompt, *_ = video_row(conn, media)
    assert (
        api.send_message.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        == f"own:gen:{video_id}"
    )
    event(video=media, caption="/work", update_id=500)
    assert api.send_message.call_count == 3
    event(video=media, caption="/work")
    api.send_message.assert_called_with(12, "This video was already added.")
    event(action=f"own:gen:{video_id}")
    api.send_message.assert_called_with(12, "Add the interview text first.")
    event(action=f"own:skip:{video_id}")
    event(text="Closed", reply_to_message={"message_id": prompt})
    assert video_row(conn, media)[2:] == ("", "skipped", None)
    model.complete.assert_not_called()


def test_reply_join_dedupe_and_unknown_fallback(bot, conn, media, monkeypatch):
    api, model, event = bot
    event(document={**media, "mime_type": "video/mp4"}, caption="/work")
    _, prompt, *_ = video_row(conn, media)
    reply = {"reply_to_message": {"message_id": prompt}}
    event(text="Питання", update_id=501, **reply)
    event(text="Питання", update_id=501, **reply)
    api.get_file.return_value = "files/test.txt"
    api.download_file.return_value = "Відповідь".encode()
    event(document={"file_id": "txt", "file_name": "text.TXT"}, **reply)
    assert video_row(conn, media)[2] == "Питання\n\nВідповідь"
    api.get_file.assert_called_once_with("txt")
    api.download_file.assert_called_once_with("files/test.txt")
    api.send_message.assert_called_with(12, "Text added (18 chars).")
    event(text="a" * 20_000, **reply)
    api.send_message.assert_called_with(12, "Interview exceeds 20,000 characters.")
    model.complete.return_value = "Übersetzt"
    event(text="Окремий допис", reply_to_message={"message_id": -1})
    assert " · telegram" in api.send_message.call_args.args[1]

    model.complete.return_value = '{"de":"Text","ua":"Текст"}'
    monkeypatch.setattr(
        "postpilot.sources.own_video.humanize_pair",
        Mock(return_value=("Text", "Текст")),
    )
    event(action=f"own:gen:{video_row(conn, media)[0]}")
    assert video_row(conn, media)[3] == "generated"
    assert "AI label: no" in api.send_message.call_args.args[1]


@pytest.mark.parametrize("step", ["AI", "JSON", "humanizer", "length"])
def test_generation_failure(bot, conn, media, monkeypatch, step):
    api, model, event = bot
    model.complete.return_value = {
        "JSON": "bad",
        "length": json.dumps({"de": "a" * 3001, "ua": "Текст"}),
    }.get(step, '{"de":"Text","ua":"Текст"}')
    if step == "AI":
        model.complete.side_effect = AIError("unavailable")
    humanizer = Mock(side_effect=HumanizerError("rejected"))
    monkeypatch.setattr("postpilot.sources.own_video.humanize_pair", humanizer)
    with conn.transaction():
        before = conn.execute("SELECT count(*) FROM items").fetchone()
    event(video=media, caption="/work ai Інтерв’ю")
    assert video_row(conn, media)[3:] == ("collecting", None)
    assert (
        api.send_message.call_args.args[1]
        == f"Generation failed ({step}), press Generate again."
    )
    with conn.transaction():
        assert conn.execute("SELECT count(*) FROM items").fetchone() == before


def test_generation_regeneration_and_approval(bot, conn, repo, media, monkeypatch):
    api, model, event = bot
    model.complete.return_value = '{"de":"Text","ua":"Текст"}'
    humanizer = Mock(return_value=("Deutsch", "Українська"))
    monkeypatch.setattr("postpilot.sources.own_video.humanize_pair", humanizer)
    event(video=media, caption="/work ai </interview>")
    _, _, _, status, item_id = video_row(conn, media)
    assert "&lt;/interview&gt;" in model.complete.call_args.args[1]
    assert status == "generated"
    item = repo.get_item(item_id)
    assert isinstance(item, Item)
    assert (item.source, item.state) == ("own_video", ItemState.AWAITING_APPROVAL)
    assert [v.text for v in repo.get_variants(item_id)] == ["Deutsch", "Українська"]
    api.send_video.assert_called_once_with(12, "file")
    preview = api.send_message.call_args.args[1]
    assert (
        "Own video · AI label: yes · Channels: LinkedIn DE+UA, Threads, Instagram, Facebook"
        in preview
    )
    assert "Reels skipped: video is 91 s (3-90 s allowed)" in preview

    humanizer.return_value = ("Aktualisiert", "Оновлено")
    event(action=f"regen:{item_id}", update_id=502)
    event(action=f"regen:{item_id}", update_id=502)
    assert model.complete.call_count == 2
    assert [v.text for v in repo.get_variants(item_id)] == ["Aktualisiert", "Оновлено"]

    humanizer.side_effect = HumanizerError("rejected")
    event(action=f"regen:{item_id}")
    assert "Оновлено" in api.send_message.call_args.args[1]
    assert [v.text for v in repo.get_variants(item_id)] == ["Aktualisiert", "Оновлено"]

    event(action=f"approve:{item_id}")
    assert repo.get_item(item_id).state == ItemState.APPROVED
    assert repo.next_video_variant() is None
    assert repo.next_video_variant(retry=True) is None
    channel = Mock(name="test-channel")
    assert (
        Publisher(repo, [channel]).publish_item(item_id).channels[channel.name].outcome
        == "held"
    )
    channel.publish.assert_not_called()
