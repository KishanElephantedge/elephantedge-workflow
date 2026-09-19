"""Test fixtures.

Two rules this harness exists to enforce, both learned from real failures in this project:

1. NO TEST EVER SPENDS MONEY. Every paid provider call (Apify, Deepline, LLM) must be
   monkeypatched by the test. There is no "live" mode here at all -- a test that reaches a
   real provider is a bug in the test, not a feature.

2. NO TEST EVER RUNS AGAINST AN EMPTY DATABASE AND CALLS THAT A PASS. On 2026-09-18 a whole
   afternoon of "confirmed fixed, chain runs in milliseconds" results turned out to have run
   against a local, essentially empty Postgres -- every stage returned zero and looked like it
   passed. Tests here build their own real rows explicitly, and assert on them.

The engine is in-memory SQLite with only the tables a given test needs (`db` takes an explicit
table list). Full `Base.metadata.create_all` does not work -- the model set is split across
app/db/models.py and app/gtm_os/**, so metadata is only complete if every module is imported,
which is slow and pulls in provider clients. Per-test table lists keep tests fast and honest
about what they actually touch.
"""
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_factory():
    """Returns make(tables) -> Session, for the tables a test actually needs.

    Example:
        from app.db.models import Company, Batch
        from app.gtm_os.intelligence.signal import GtmSignal
        db = db_factory([Company, Batch, GtmSignal])
    """
    engines = []

    def make(models):
        engine = sa.create_engine("sqlite:///:memory:")
        engines.append(engine)
        sa.orm.configure_mappers()
        tables = [m.__table__ for m in models]
        for m in models:
            m.__table__.metadata.create_all(engine, tables=tables)
        return sessionmaker(bind=engine)()

    yield make
    for e in engines:
        e.dispose()


@pytest.fixture(autouse=True)
def _no_real_provider_calls(monkeypatch):
    """Belt-and-braces: fail loudly if a test reaches a real provider instead of silently
    spending. Tests that legitimately exercise a provider path monkeypatch the specific
    function they need; anything left unpatched raises here rather than opening a socket."""
    import httpx

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "A test attempted a real outbound HTTP call. Tests must never spend money -- "
            "monkeypatch the provider function this code path uses."
        )

    monkeypatch.setattr(httpx, "post", _blocked, raising=False)
    monkeypatch.setattr(httpx, "get", _blocked, raising=False)
