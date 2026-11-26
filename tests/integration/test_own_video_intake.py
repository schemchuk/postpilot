import itertools
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest
import test_store

from postpilot.ai.client import AIError
from postpilot.ai.humanizer import HumanizedPost, HumanizerError
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
def clock():
    return Mock(return_value=1_000.0)


@pytest.fixture
def bot(repo, clock):
    api, model = Mock(), Mock()
    api.send_message.side_effect = lambda *args, **kwargs: next(ids)
    handler = ApprovalHandler(
        repo,
        api,
        12,
        inbox=TelegramInbox(repo, api, model),
        own_video=OwnVideoInbox(repo, api, model, clock=clock),
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
        return header["message_id"]

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
    event(text="/work ai")
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
    message_id = event(action=f"own:gen:{video_id}")
    api.send_message.assert_called_with(
        12, "Add the interview text first.", reply_to_message_id=message_id
    )
    api.answer_callback_query.assert_not_called()
    event(action=f"own:skip:{video_id}")
    event(text="Closed", reply_to_message={"message_id": prompt})
    assert video_row(conn, media)[2:] == ("", "skipped", None)
    # A skipped video gets a new ID, so the old buttons stay stale.
    event(video=media, caption="/work")
    assert video_row(conn, media)[2:] == ("", "collecting", None)
    message_id = event(action=f"own:gen:{video_id}")
    api.send_message.assert_called_with(
        12, "Already handled", reply_to_message_id=message_id
    )
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
        Mock(return_value=(HumanizedPost("Text"), HumanizedPost("Текст"))),
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
    humanizer = Mock(
        return_value=(HumanizedPost("Deutsch"), HumanizedPost("Українська"))
    )
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
    assert "Channels: LinkedIn DE, Threads\nOwn video · AI label: yes" in preview
    assert "UA (not published: LinkedIn UA paused)\nУкраїнська" in preview
    assert "Reels skipped: video is 91 s (3-90 s allowed)" in preview

    humanizer.return_value = (HumanizedPost("Aktualisiert"), HumanizedPost("Оновлено"))
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


def test_generate_does_not_answer_callback_query(bot, conn, media, monkeypatch):
    api, model, event = bot
    event(video=media, caption="/work")
    _, prompt, *_ = video_row(conn, media)
    event(text="Interview", reply_to_message={"message_id": prompt})
    model.complete.return_value = '{"de":"Text","ua":"Текст"}'
    monkeypatch.setattr(
        "postpilot.sources.own_video.humanize_pair",
        Mock(return_value=(HumanizedPost("Text"), HumanizedPost("Текст"))),
    )
    event(action=f"own:gen:{video_row(conn, media)[0]}")
    assert video_row(conn, media)[3] == "generated"
    api.answer_callback_query.assert_not_called()
    assert "Own video · AI label: no" in api.send_message.call_args.args[1]


def buttons(api, prompt):
    args = api.send_message.call_args.args
    assert args[1] == prompt
    keyboard = args[2]["inline_keyboard"]
    result = {
        button["text"]: button["callback_data"] for row in keyboard for button in row
    }
    assert all(len(data.encode()) <= 64 for data in result.values())
    return result


_KIND = "What kind of video?"
_VIDEO = "Send the video (MP4 up to 20 MB; Reels need 3-90 s)."
_DESCRIPTION = "Video received."
_TEXT = "Send the text as messages or a .txt file, then press Done."


def test_guided_video_generates_preview(bot, conn, media, monkeypatch):
    api, model, event = bot
    model.complete.return_value = '{"de":"Text","ua":"Текст"}'
    humanizer = Mock(return_value=(HumanizedPost("Text"), HumanizedPost("Текст")))
    monkeypatch.setattr("postpilot.sources.own_video.humanize_pair", humanizer)

    event(text="/work@bot")
    kind = buttons(api, _KIND)
    event(text="Before choosing kind")
    api.send_message.assert_called_with(12, _KIND)
    event(action=kind["AI-generated"])
    buttons(api, _VIDEO)
    event(text="Before uploading video", reply_to_message={"message_id": -1})
    api.send_message.assert_called_with(12, _VIDEO)
    event(video=media, caption="Other caption")
    event(video=media, reply_to_message={"message_id": -1})
    assert video_row(conn, media) is None
    event(video=media)
    description = buttons(api, _DESCRIPTION)
    event(text="Before choosing description")
    api.send_message.assert_called_with(
        12, "Choose Upload description, No description, or Cancel."
    )
    message_id = event(action=description["No description"])
    api.send_message.assert_called_with(
        12, "Coming soon", reply_to_message_id=message_id
    )
    model.complete.assert_not_called()
    assert video_row(conn, media)[3:] == ("collecting", None)
    event(action=description["Upload description"])
    text = buttons(api, _TEXT)
    message_id = event(action=text["Done"])
    api.send_message.assert_called_with(
        12, "Add the text first.", reply_to_message_id=message_id
    )
    model.complete.assert_not_called()
    event(text="Питання", update_id=700)
    api.send_message.assert_called_with(12, "Text added (7 chars).")
    event(text="Питання", update_id=700)
    api.get_file.return_value = "files/answer.txt"
    api.download_file.return_value = "Відповідь".encode()
    event(document={"file_id": "description", "file_name": "answer.TXT"})
    api.send_message.assert_called_with(12, "Text added (18 chars).")
    event(action=text["Done"])
    _, _, interview, status, item_id = video_row(conn, media)
    assert (interview, status) == ("Питання\n\nВідповідь", "generated")
    assert item_id is not None
    assert "Own video · AI label: yes" in api.send_message.call_args.args[1]
    assert "Reels skipped: video is 91 s" in api.send_message.call_args.args[1]
    assert humanizer.call_args.kwargs["soft"] is True
    api.send_video.assert_called_once_with(12, "file")
    event(text="Питання", update_id=700)
    message_id = event(action=text["Done"])
    api.send_message.assert_called_with(
        12, "Already handled", reply_to_message_id=message_id
    )
    model.complete.assert_called_once()
    api.answer_callback_query.assert_not_called()


@pytest.mark.parametrize("step", [_KIND, _VIDEO, _DESCRIPTION, _TEXT])
def test_cancel_guided_intake_at_each_step(bot, conn, media, step):
    api, model, event = bot
    event(text="/work")
    current = buttons(api, _KIND)
    if step != _KIND:
        event(action=current["Original"])
        current = buttons(api, _VIDEO)
    if step in {_DESCRIPTION, _TEXT}:
        event(document={**media, "mime_type": "video/mp4"})
        current = buttons(api, _DESCRIPTION)
    if step == _TEXT:
        event(action=current["Upload description"])
        current = buttons(api, _TEXT)
    message_id = event(action=current["Cancel"])
    api.edit_message_text.assert_called_once_with(12, message_id, "Cancelled")
    message_id = event(action=current["Cancel"])
    api.send_message.assert_called_with(
        12, "Already handled", reply_to_message_id=message_id
    )
    row = video_row(conn, media)
    if step in {_KIND, _VIDEO}:
        assert row is None
    else:
        assert row[3:] == ("skipped", None)
    model.complete.return_value = "Übersetzt"
    event(text="Окремий допис")
    assert " · telegram" in api.send_message.call_args.args[1]


def test_guided_expiry_uses_last_step_and_releases_text_to_s2(bot, media, clock):
    api, model, event = bot
    event(text="/work")
    kind = buttons(api, _KIND)
    event(action=kind["Original"])
    event(video=media)
    event(action=buttons(api, _DESCRIPTION)["Upload description"])
    text = buttons(api, _TEXT)
    clock.return_value += 3599
    event(text="Ще активно")
    api.send_message.assert_called_with(12, "Text added (10 chars).")
    clock.return_value += 3599
    model.complete.assert_not_called()
    clock.return_value += 1
    message_id = event(action=text["Done"])
    api.send_message.assert_called_with(
        12, "Expired, send /work again.", reply_to_message_id=message_id
    )
    model.complete.return_value = "Übersetzt"
    event(text="Окремий допис")
    assert " · telegram" in api.send_message.call_args.args[1]


def test_new_work_replaces_guided_intake(bot, conn, media):
    api, model, event = bot
    event(text="/work")
    event(action=buttons(api, _KIND)["Original"])
    event(video=media)
    description = buttons(api, _DESCRIPTION)
    event(text="/work", update_id=701)
    current = buttons(api, _KIND)
    event(text="/work", update_id=701)
    assert buttons(api, _KIND) == current
    assert video_row(conn, media)[3:] == ("skipped", None)
    message_id = event(action=description["Upload description"])
    api.send_message.assert_called_with(
        12, "Already handled", reply_to_message_id=message_id
    )
    event(action=current["AI-generated"])
    buttons(api, _VIDEO)
    # The replaced intake's video was never posted, so the same file is taken again.
    event(video=media)
    buttons(api, _DESCRIPTION)
    assert video_row(conn, media)[3:] == ("collecting", None)
    model.complete.assert_not_called()
