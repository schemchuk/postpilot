import pytest

from postpilot.store import db

DIRECT_URL = "postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/db"
POOLER_URL = "postgresql://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/db"
POOLER_DSN = "host=ep-x-pooler.eu-central-1.aws.neon.tech dbname=db user=u password=p"


@pytest.fixture
def connect_calls(monkeypatch):
    calls = []

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    return calls


@pytest.mark.parametrize("url", [POOLER_URL, POOLER_DSN])
def test_schema_requires_direct_host(url, connect_calls):
    with pytest.raises(ValueError, match="need a direct, non-pooler host"):
        db.connect(url, schema="test_abc")

    assert connect_calls == []


def test_schema_sets_startup_search_path(connect_calls):
    db.connect(DIRECT_URL, schema="test_abc")

    assert connect_calls == [((DIRECT_URL,), {"options": "-c search_path=test_abc"})]


def test_invalid_schema_is_rejected_before_connect(connect_calls):
    with pytest.raises(ValueError, match="Invalid schema name"):
        db.connect(DIRECT_URL, schema="bad; drop")

    assert connect_calls == []


@pytest.mark.parametrize("url", [DIRECT_URL, POOLER_URL])
def test_connect_without_schema(url, connect_calls):
    db.connect(url)

    assert connect_calls == [((url,), {})]


def test_connect_uses_database_url_from_environment(monkeypatch, connect_calls):
    monkeypatch.setenv("DATABASE_URL", DIRECT_URL)

    db.connect(url=None)

    assert connect_calls == [((DIRECT_URL,), {})]
