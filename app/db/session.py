from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

# pool_pre_ping guards against Neon's pooled ("-pooler") endpoint handing back a stale
# connection after the app has been idle -- see synefi/app/db/session.py for the full
# explanation; same fix applied here since this backend hits the same endpoint.
#
# connect_timeout/statement_timeout added after a real, reproducible pattern: a single
# request times out completely, and the very next one succeeds immediately -- consistent
# with Neon's own compute occasionally taking a while to resume from auto-suspend on the
# first query after idle, with nothing here bounding how long that wait (or any other
# single slow query) could hang a request thread for. Neither engine.execute nor
# pool_pre_ping's own check had any timeout before this -- a genuinely stuck connection
# attempt or query could block forever. This doesn't fix Neon's wake-up latency itself
# (that's a Neon-side setting, not something fixable here), it just turns an unbounded
# hang into a clean, fast, catchable error instead.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10, "options": "-c statement_timeout=15000"},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Indexes/tables this backend owns (campaign_events -- the SalesRobot webhook table;
# calendar_bookings -- Google Calendar appointment sync). Shared-table indexes (companies,
# contacts, etc.) are ensured by Synefi's backend, the schema owner -- see
# synefi/app/db/session.py for why this is raw SQL rather than an Alembic migration.
def ensure_indexes():
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaign_events_contact_id ON campaign_events (contact_id)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS calendar_bookings (
                id SERIAL PRIMARY KEY,
                google_event_id VARCHAR NOT NULL UNIQUE,
                booker_name VARCHAR,
                booker_email VARCHAR,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                status VARCHAR,
                raw_payload JSON NOT NULL,
                synced_at TIMESTAMP
            )
        """))
