from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

# pool_pre_ping guards against Neon's pooled ("-pooler") endpoint handing back a stale
# connection after the app has been idle -- see synefi/app/db/session.py for the full
# explanation; same fix applied here since this backend hits the same endpoint.
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Indexes for the tables this backend owns (campaign_events -- the SalesRobot webhook table).
# Shared-table indexes (companies, contacts, etc.) are ensured by Synefi's backend, the schema
# owner -- see synefi/app/db/session.py for why this is CREATE INDEX IF NOT EXISTS rather than
# an Alembic migration.
def ensure_indexes():
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_campaign_events_contact_id ON campaign_events (contact_id)"))
