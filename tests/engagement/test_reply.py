from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from postpilot.engagement.job import comment_message
from postpilot.engagement.store import EngagementEvent, EngagementStore
from postpilot.engagement.threads import ThreadsReplyClient, ThreadsReplyError
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.telegram import TelegramApi, TelegramError

NOW = datetime.now(UTC)
POST = "https://www.threads.com/@reader/post/comment"
LINK = "https://www.threads.com/@author/post/reply"


@pytest.fixture
def reply():
    event = EngagementEvent(
        uuid4(),
        "threads",
        "comment",
        "comment-id",
        "Reader",
        "<comment>",
        POST,
        "<draft>",
        NOW,
        NOW,
        32,
        NOW,
    )
    store = Mock(spec=EngagementStore)
    store.get.side_effect = lambda _: store.event
    store.find_by_message_id.side_effect = lambda id: store.event if id == 32 else None
    store.event = event

    def update(**fields):
        store.event = replace(store.event, **fields)
        return store.event

    def claim(_, now):
        current = store.event
        if current.reply_status == "SENT" or (
            current.reply_status == "SENDING"
            and current.reply_claimed_at >= now - timedelta(minutes=5)
        ):
            return None
        return update(reply_status="SENDING", reply_claimed_at=now)

    store.claim_reply.side_effect = claim
    store.save_reply_container.side_effect = lambda _, id: update(reply_container_id=id)
    store.mark_reply_sent.side_effect = lambda _, id, now: update(
        reply_status="SENT", reply_remote_id=id, reply_sent_at=now
    )
    store.mark_reply_failed.side_effect = lambda _: update(reply_status="FAILED")
    store.set_reply_draft.side_effect = lambda _, text: update(reply_draft=text)

    requests = []
    responses = []

    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture"
        assert "fixture" not in str(request.url)
        if request.url.path.endswith("/me/threads_publish"):
            assert store.event.reply_container_id == "container"
            assert parse_qs(request.content.decode()) == {"creation_id": ["container"]}
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with httpx.Client(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as http:
        factory = Mock(return_value=ThreadsReplyClient(http, "fixture"))
        api = Mock(spec=TelegramApi)
        handler = ApprovalHandler(
            Mock(),
            api,
            42,
            engagement=store,
            inbox=Mock(),
            drafter=Mock(),
            threads_reply_client=factory,
        )

        def press(action="send", sender=42, chat=42, message_id=32):
            handler.handle_update(
                {
                    "callback_query": {
                        "id": "query",
                        "data": f"eng:{action}:{event.id}",
                        "from": {"id": sender},
                        "message": {"chat": {"id": chat}, "message_id": message_id},
                    }
                }
            )

        yield handler, store, factory, requests, responses, press


def ok(**fields):
    return httpx.Response(200, json=fields)


def test_send_persists_container_before_publish_and_edits_telegram(reply):
    handler, store, _, requests, responses, press = reply
    _, keyboard = comment_message(store.event)
    assert [b["text"] for b in keyboard["inline_keyboard"][0]] == [
        "Open post",
        "Another variant",
        "Send",
    ]
    responses += [ok(id="container"), ok(id="remote"), ok(permalink=LINK)]
    press()
    assert [r.url.path for r in requests] == [
        "/v1.0/me/threads",
        "/v1.0/me/threads_publish",
        "/v1.0/remote",
    ]
    assert parse_qs(requests[0].content.decode()) == {
        "media_type": ["TEXT"],
        "text": ["<draft>"],
        "reply_to_id": ["comment-id"],
    }
    assert requests[2].url.params["fields"] == "permalink"
    assert store.event.reply_status == "SENT"
    assert store.event.reply_remote_id == "remote"
    handler.api.edit_message_text.assert_called_once_with(
        42,
        32,
        f"Reader · threads\n\n<i>&lt;comment&gt;</i>\n\n"
        f"<code>&lt;draft&gt;</code>\n\nSent: {LINK}",
        parse_mode="HTML",
        reply_markup={"inline_keyboard": [[{"text": "Open post", "url": POST}]]},
    )
    handler.api.answer_callback_query.assert_not_called()
    handler.api.send_message.assert_not_called()


def test_sent_reply_falls_back_to_chat_if_preview_edit_fails(reply):
    handler, store, _, _, responses, press = reply
    handler.api.edit_message_text.side_effect = TelegramError("edit failed")
    responses += [ok(id="container"), ok(id="remote"), ok(permalink=LINK)]

    press()

    assert store.event.reply_status == "SENT"
    handler.api.answer_callback_query.assert_not_called()
    handler.api.send_message.assert_called_once_with(42, "Sent", reply_to_message_id=32)


@pytest.mark.parametrize(
    "status,answer", [("SENDING", "Sending in progress"), ("SENT", "Already sent")]
)
def test_duplicate_press_does_not_read_token_or_contact_threads(reply, status, answer):
    handler, store, factory, requests, _, press = reply
    store.event = replace(store.event, reply_status=status, reply_claimed_at=NOW)
    press()
    factory.assert_not_called()
    assert not requests
    handler.api.send_message.assert_called_once_with(42, answer, reply_to_message_id=32)
    handler.api.answer_callback_query.assert_not_called()


@pytest.mark.parametrize(
    "status", ["PUBLISHED", "FINISHED", "ERROR", "EXPIRED", "unknown", "IN_PROGRESS"]
)
def test_stale_claim_recovers_using_container_status(reply, status):
    handler, store, _, requests, responses, press = reply
    store.event = replace(
        store.event,
        reply_status="SENDING",
        reply_container_id="container",
        reply_claimed_at=NOW - timedelta(minutes=6),
    )
    responses.append(ok(status=status))
    if status not in {"PUBLISHED", "IN_PROGRESS"}:
        if status != "FINISHED":
            responses.append(ok(id="container"))
        responses += [ok(id="remote"), httpx.Response(503)]
    press()
    assert requests[0].url.path == "/v1.0/container"
    assert requests[0].url.params["fields"] == "status"
    if status == "IN_PROGRESS":
        assert len(requests) == 1
        assert store.event.reply_status == "SENDING"
    else:
        assert sum(r.url.path.endswith("/threads_publish") for r in requests) == (
            0 if status == "PUBLISHED" else 1
        )
        assert store.event.reply_status == "SENT"
        assert store.event.reply_remote_id == (
            None if status == "PUBLISHED" else "remote"
        )
        assert handler.api.edit_message_text.call_args.args[2].endswith(f"Sent: {POST}")


def test_definite_error_can_retry(reply):
    handler, store, _, requests, responses, press = reply
    responses += [
        ok(id="container"),
        httpx.Response(400, json={"error": {"code": 100}}),
    ]
    press()
    assert store.event.reply_status == "FAILED"
    handler.api.send_message.assert_called_once_with(
        42, "Send failed", reply_to_message_id=32
    )
    handler.api.answer_callback_query.assert_not_called()
    handler.api.edit_message_text.assert_not_called()
    responses += [ok(status="FINISHED"), ok(id="remote"), ok(permalink=LINK)]
    press()
    assert len(requests) == 5
    assert store.event.reply_status == "SENT"


@pytest.mark.parametrize(
    "failure,status,answer",
    [
        (
            httpx.Response(403, json={"error": {"code": 10}}),
            "FAILED",
            "Token lacks threads_manage_replies, re-issue it (runbook)",
        ),
        (httpx.Response(503), "SENDING", "Send uncertain, press again in 5 minutes"),
        (
            httpx.ReadTimeout("private"),
            "SENDING",
            "Send uncertain, press again in 5 minutes",
        ),
        (
            httpx.Response(200, text="invalid JSON"),
            "SENDING",
            "Send uncertain, press again in 5 minutes",
        ),
    ],
)
def test_publish_failure_classification(reply, failure, status, answer):
    handler, store, _, _, responses, press = reply
    responses += [ok(id="container"), failure]
    press()
    assert store.event.reply_status == status
    handler.api.send_message.assert_called_once_with(42, answer, reply_to_message_id=32)
    handler.api.answer_callback_query.assert_not_called()
    handler.api.edit_message_text.assert_not_called()


def test_missing_token_marks_failed_without_http(reply):
    handler, store, factory, requests, _, press = reply
    factory.return_value = None
    press()
    assert not requests
    assert store.event.reply_status == "FAILED"
    handler.api.send_message.assert_called_once_with(
        42, "Threads token unavailable", reply_to_message_id=32
    )
    handler.api.answer_callback_query.assert_not_called()


@pytest.mark.parametrize("sender,chat,id", [(99, 42, 32), (42, 99, 32), (42, 42, 99)])
def test_mismatched_callback_is_ignored(reply, sender, chat, id):
    handler, store, factory, _, _, press = reply
    press(sender=sender, chat=chat, message_id=id)
    store.claim_reply.assert_not_called()
    factory.assert_not_called()
    handler.api.answer_callback_query.assert_not_called()


@pytest.mark.parametrize("text", [" ", "x" * 501])
def test_invalid_reply_length_is_rejected_before_http(reply, text):
    _, _, factory, requests, _, _ = reply
    with pytest.raises(ThreadsReplyError, match="Invalid Threads reply length"):
        factory().create_reply_container("comment-id", text)
    assert not requests


@pytest.mark.parametrize(
    "message_id,status,text,answer",
    [
        (32, None, "  Мій <текст>\n  другий рядок  ", None),
        (32, "SENT", "New draft", "Reply already sent"),
        (32, "SENDING", "New draft", "Reply already sent"),
        (32, None, "x" * 501, "Reply exceeds 500 characters"),
        (32, None, "   ", "Reply is empty"),
        (99, None, "New post", None),
    ],
)
def test_edit_by_reply_routes_before_pending_edit_and_inbox(
    reply, message_id, status, text, answer
):
    handler, store, factory, _, _, _ = reply
    store.event = replace(store.event, reply_status=status)
    handler.repo.has_inbox_update.return_value = False
    handler.repo.apply_pending_edit.return_value = None
    handler.handle_update(
        {
            "update_id": 1,
            "message": {
                "from": {"id": 42},
                "chat": {"id": 42},
                "text": text,
                "reply_to_message": {"message_id": message_id},
            },
        }
    )
    factory.assert_not_called()
    if message_id != 32:
        handler.inbox.handle_text.assert_called_once_with(1, 42, text)
        store.set_reply_draft.assert_not_called()
        return
    handler.repo.apply_pending_edit.assert_not_called()
    handler.inbox.handle_text.assert_not_called()
    if answer:
        handler.api.send_message.assert_called_once_with(42, answer)
        store.set_reply_draft.assert_not_called()
        handler.api.edit_message_text.assert_not_called()
    else:
        assert store.event.reply_draft == text.strip()
        rendered, markup = comment_message(store.event)
        assert "Мій &lt;текст&gt;\n  другий рядок" in rendered
        handler.api.edit_message_text.assert_called_once_with(
            42,
            32,
            rendered,
            parse_mode="HTML",
            reply_markup=markup,
        )


@pytest.mark.parametrize("status", ["SENT", "SENDING"])
def test_redraft_refused_after_claim(reply, status):
    handler, store, _, _, _, press = reply
    store.event = replace(store.event, reply_status=status)
    press(action="redraft")
    handler.drafter.redraft.assert_not_called()
    store.set_reply_draft.assert_not_called()
    handler.api.send_message.assert_called_once_with(
        42, "Reply already sent", reply_to_message_id=32
    )
    handler.api.answer_callback_query.assert_not_called()


def test_unavailable_container_status_keeps_claim_for_recovery(reply):
    handler, store, _, requests, responses, press = reply
    store.event = replace(
        store.event,
        reply_status="SENDING",
        reply_container_id="container",
        reply_claimed_at=NOW - timedelta(minutes=6),
    )
    responses.append(httpx.Response(503))
    press()
    assert len(requests) == 1
    assert store.event.reply_status == "SENDING"
    handler.api.send_message.assert_called_once_with(
        42, "Send uncertain, press again in 5 minutes", reply_to_message_id=32
    )
    handler.api.answer_callback_query.assert_not_called()
