from datetime import UTC, datetime, timedelta

import pytest
import test_store

from postpilot.store.repo import ItemRepo, LostPublicationClaim
from postpilot.video.publications import VideoPublicationStore
from postpilot.video.store import VideoStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn

STALE_AFTER = timedelta(minutes=15)


def test_approval_claim_stale_and_recovery(conn):
    renders = VideoStore(conn)
    publications = VideoPublicationStore(conn)
    render_id = renders.claim(ItemRepo(conn).create_item("github").id, "de")
    assert publications.oldest_eligible({"fb"}, STALE_AFTER) is None
    assert publications.claim(render_id, "fb", STALE_AFTER) is None
    assert renders.attach(render_id, "heygen", "script", b"labelled", 10)
    assert publications.claim(render_id, "fb", STALE_AFTER) is None
    assert renders.approve(render_id, 1, datetime.now(UTC))
    assert publications.oldest_eligible({"fb", "ig"}, STALE_AFTER) == render_id
    assert publications.oldest_eligible(set(), STALE_AFTER) is None

    claim = publications.claim(render_id, "fb", STALE_AFTER)
    assert claim and not claim.recovered and not claim.stale
    publications.save_ref(claim, "remote")
    assert publications.claim(render_id, "fb", STALE_AFTER) is None
    assert publications.oldest_eligible({"fb"}, STALE_AFTER) is None
    with conn.transaction():
        conn.execute(
            "UPDATE video_publications SET updated_at = now() - interval '1 hour'"
        )

    assert publications.oldest_eligible({"fb"}, STALE_AFTER) == render_id
    recovered = publications.claim(render_id, "fb", STALE_AFTER)
    assert recovered and recovered.stale and recovered.recovered
    assert recovered.remote_ref == "remote"
    assert recovered.remote_ref_claimed_at == claim.updated_at
    publications.save_ref(recovered, "remote")
    with conn.transaction():
        timestamp = conn.execute(
            "SELECT remote_ref_claimed_at FROM video_publications"
        ).fetchone()[0]
    assert timestamp == claim.updated_at
    with pytest.raises(LostPublicationClaim):
        publications.mark_published(claim, "remote")

    publications.release(recovered)
    retry = publications.claim(render_id, "fb", STALE_AFTER)
    assert retry and retry.remote_ref == "remote"
    publications.mark_failed(retry, "safe error")
    assert publications.claim(render_id, "fb", STALE_AFTER) is None
    assert publications.oldest_eligible({"fb"}, STALE_AFTER) is None
    assert publications.oldest_eligible({"fb"}, STALE_AFTER, retry=True) == render_id
    retry = publications.claim(render_id, "fb", STALE_AFTER, retry=True)
    assert retry and retry.recovered and not retry.stale
    assert retry.remote_ref == "remote"
    publications.mark_published(retry, "remote")
    assert publications.claim(render_id, "fb", STALE_AFTER) is None
    assert publications.oldest_eligible({"fb"}, STALE_AFTER) is None
    assert publications.oldest_eligible({"fb", "ig"}, STALE_AFTER) == render_id

    other = publications.claim(render_id, "ig", STALE_AFTER)
    publications.release(other)
    assert not publications.claim(render_id, "ig", STALE_AFTER).recovered


def test_failed_and_active_claims_do_not_block_newer_render(conn):
    renders = VideoStore(conn)
    publications = VideoPublicationStore(conn)
    items = ItemRepo(conn)

    def approve():
        render_id = renders.claim(items.create_item("github").id, "de")
        assert renders.attach(render_id, "heygen", "script", b"labelled", 10)
        assert renders.approve(render_id, 1, datetime.now(UTC))
        return render_id

    older, newer = approve(), approve()
    claim = publications.claim(older, "fb", STALE_AFTER)
    assert publications.oldest_eligible({"fb"}, STALE_AFTER) == newer
    publications.mark_failed(claim, "permanent")
    assert publications.oldest_eligible({"fb"}, STALE_AFTER) == newer
    assert publications.oldest_eligible({"fb"}, STALE_AFTER, retry=True) == older


def test_linkedin_claim_requires_opt_in_and_approval(conn):
    renders = VideoStore(conn)
    publications = VideoPublicationStore(conn)
    items = ItemRepo(conn)
    channels = {"linkedin-video", "linkedin-ua-video"}

    off = renders.claim(items.create_item("github").id, "de")
    assert renders.attach(off, "off", "Script", b"video", 20)
    assert renders.approve(off, 1, datetime.now(UTC))
    assert publications.oldest_eligible(channels, STALE_AFTER) is None
    for channel in channels:
        assert publications.claim(off, channel, STALE_AFTER, retry=True) is None
    # Reels stay eligible for a render without the LinkedIn opt-in.
    assert publications.claim(off, "facebook-reels", STALE_AFTER) is not None

    on = renders.claim(items.create_item("github").id, "de")
    assert renders.attach(on, "on", "Script", b"video", 20)
    assert renders.toggle_linkedin(on) is True
    assert publications.oldest_eligible(channels, STALE_AFTER) is None
    assert publications.claim(on, "linkedin-video", STALE_AFTER) is None
    assert renders.approve(on, 1, datetime.now(UTC))
    assert publications.oldest_eligible(channels, STALE_AFTER) == on
    for channel in channels:
        claim = publications.claim(on, channel, STALE_AFTER)
        assert claim is not None
        publications.mark_published(claim, f"post:{channel}")
    assert publications.oldest_eligible(channels, STALE_AFTER) is None
