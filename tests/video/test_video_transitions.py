from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from postpilot.video.store import VideoStatus, VideoStore


@pytest.mark.parametrize(
    ("method", "source", "target", "kwargs"),
    [
        ("approve", "READY", "APPROVED", {}),
        ("reject", "READY", "REJECTED", {}),
        ("attach", "RENDERING", "READY", {}),
        ("mark_failed", "RENDERING", "FAILED", {}),
        ("mark_failed", "READY", "FAILED", {"preview": True}),
    ],
)
@pytest.mark.parametrize("status", list(VideoStatus))
def test_atomic_transition_is_guarded(method, source, target, kwargs, status):
    conn = MagicMock()
    store = VideoStore(conn)
    render_id = uuid4()
    now = datetime(2026, 11, 19, tzinfo=UTC)
    args = {
        "approve": (123, now),
        "reject": (123,),
        "attach": ("remote", "script", b"mp4", 10.0),
        "mark_failed": (),
    }

    def execute(query, params):
        assert query.strip().startswith(f"UPDATE video_renders SET status = '{target}'")
        if method == "mark_failed":
            assert query.strip().endswith("WHERE id = %s AND status = %s")
            assert params == (render_id, source)
        else:
            assert query.strip().endswith(f"WHERE id = %s AND status = '{source}'")
            assert params[-1] == render_id
        return MagicMock(rowcount=int(status == source))

    conn.execute.side_effect = execute
    assert getattr(store, method)(render_id, *args[method], **kwargs) is (
        status == source
    )
    conn.execute.assert_called_once()
    conn.transaction.assert_called_once_with()


def test_month_uses_utc_boundaries_and_counts_rendering():
    from datetime import timedelta, timezone

    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (2,)
    west = timezone(timedelta(hours=-5))
    now = datetime(2026, 11, 30, 20, tzinfo=west)

    assert VideoStore(conn).count_this_month(now) == 2
    query, bounds = conn.execute.call_args.args
    assert bounds == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert "requested_at >= %s AND requested_at < %s AND status <> 'FAILED'" in query
