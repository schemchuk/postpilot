from uuid import UUID

import httpx
import pytest

from postpilot.channels.base import ChannelError, TransientChannelError
from postpilot.core.states import ItemState
from postpilot.trace import describe_error, error_fields, set_secrets, trace


@pytest.fixture(autouse=True)
def no_secrets():
    yield
    set_secrets(())


def line(capsys):
    return capsys.readouterr().out.strip()


def test_trace_formats_safe_values(capsys):
    item = UUID(int=1)
    trace(
        "job",
        "event",
        item=item,
        state=ItemState.APPROVED,
        count=3,
        ok=True,
        none=None,
        names=["a", "b"],
        empty=[],
        tags={"z", "y"},
    )
    assert line(capsys) == (
        f"trace job event item={item} state=APPROVED count=3 ok=True none=None"
        " names=a,b empty=- tags=y,z"
    )


def test_trace_strips_query_whitespace_and_shortens(capsys):
    trace(
        "job",
        "event",
        url="https://x.test/p?access_token=abc",
        text="a b\nc",
        long="é" * 200,
    )
    out = line(capsys)
    assert "access_token" not in out and "abc" not in out
    assert "url=https://x.test/p " in out
    assert "text=a_b_c " in out
    assert out.endswith("long=" + "é" * 79 + "…")


def test_trace_redacts_configured_secrets_and_token_shapes(capsys):
    set_secrets(["s3cret-value", ""])
    trace("job", "event", a="pre-s3cret-value", b="Bearer abc", c="123456:" + "A" * 35)
    out = line(capsys)
    assert "s3cret" not in out and "abc" not in out and "AAAA" not in out


def test_error_fields_from_http_status_error_without_token_or_query():
    request = httpx.Request(
        "POST", "https://api.telegram.org/bot123:SECRET/sendMessage?x=1"
    )
    response = httpx.Response(429, request=request)
    error = httpx.HTTPStatusError("boom SECRET", request=request, response=response)
    try:
        try:
            raise error
        except httpx.HTTPStatusError:
            raise RuntimeError("wrapped SECRET") from None
    except RuntimeError as wrapped:
        wrapped.__cause__ = error
        fields = error_fields(wrapped)
    assert fields == {
        "error": "RuntimeError",
        "http_status": 429,
        "http": "POST_/bot***/sendMessage",
    }


def test_error_fields_keeps_http_metadata_when_adapter_suppresses_context():
    request = httpx.Request("POST", "https://graph.test/upload?access_token=secret")
    response = httpx.Response(503, request=request)
    with pytest.raises(ChannelError) as caught:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise ChannelError("Upload unavailable") from None

    assert error_fields(caught.value) == {
        "error": "ChannelError",
        "http_status": 503,
        "http": "POST_/upload",
    }


def test_error_fields_for_request_error_and_plain_error():
    request = httpx.Request("GET", "https://graph.test/v1/me?access_token=t")
    fields = error_fields(httpx.ConnectTimeout("slow", request=request))
    assert fields == {
        "error": "ConnectTimeout",
        "http_error": "ConnectTimeout",
        "http": "GET_/v1/me",
    }
    assert error_fields(httpx.ConnectError("no request")) == {
        "error": "ConnectError",
        "http_error": "ConnectError",
    }
    assert error_fields(ValueError("text")) == {"error": "ValueError"}


def test_describe_error_keeps_adapter_reason_and_sanitizes_it():
    set_secrets(["registered-secret"])
    token = "123456:" + "a" * 35
    error = TransientChannelError(
        f"Request failed\n  {token} Bearer short-token registered-secret "
        "https://example.test/upload?x=private-value&y=hidden after upload"
    )
    assert describe_error(error) == (
        "TransientChannelError: Request failed *** *** *** "
        "https://example.test/upload after upload"
    )


def test_describe_foreign_error_uses_only_http_fields_from_context():
    request = httpx.Request(
        "POST", "https://api.telegram.org/bot123:secret/sendMessage?x=1"
    )
    response = httpx.Response(400, request=request)
    http_error = httpx.HTTPStatusError(
        "private body", request=request, response=response
    )
    error = ValueError("private foreign message")
    error.__context__ = http_error
    assert describe_error(error) == "ValueError: HTTP 400 POST /bot***/sendMessage"


def test_describe_error_and_trace_reason_are_capped_at_200(capsys):
    reason = describe_error(ChannelError("Request rejected. " * 20))
    assert len(reason) == 200
    trace("publish", "failed", reason=reason)
    assert line(capsys) == "trace publish failed reason=" + reason.replace(" ", "_")
