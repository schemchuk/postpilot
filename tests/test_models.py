from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from postpilot.core.models import Item, Publication, PublicationStatus, Variant
from postpilot.core.states import ItemState

NOW = datetime.now(UTC)
ITEM_ID = uuid4()


@pytest.mark.parametrize(
    ("record", "field", "value"),
    [
        (
            Item(ITEM_ID, "github", ItemState.DRAFT, NOW, NOW, None, None),
            "source",
            "telegram",
        ),
        (Variant(ITEM_ID, "ua", "Original"), "text", "Edited"),
        (
            Publication(
                ITEM_ID, "example", PublicationStatus.PUBLISHING, None, None, NOW
            ),
            "status",
            PublicationStatus.PUBLISHED,
        ),
    ],
)
def test_models_are_frozen(record, field, value):
    with pytest.raises(FrozenInstanceError):
        setattr(record, field, value)


@pytest.mark.parametrize("member", [*ItemState, *PublicationStatus])
def test_enum_round_trip(member):
    assert str(member) == member.name
    assert type(member)(str(member)) is member


def test_publication_statuses_are_item_states():
    assert set(PublicationStatus) == {
        ItemState.PUBLISHING,
        ItemState.PUBLISHED,
        ItemState.FAILED,
        ItemState.SKIPPED,
    }
