from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.config import settings

# pool_pre_ping guards against Neon's pooled ("-pooler") endpoint handing back a stale
# connection after the app has been idle -- see synefi/app/db/session.py for the full
# explanation; same fix applied here since this backend hits the same endpoint.
#
# connect_timeout bounds how long opening a new connection can hang (real, reproducible
# pattern: a single request times out completely, the very next one succeeds immediately --
# consistent with Neon's compute occasionally taking a while to resume from auto-suspend).
#
# statement_timeout is NOT passed via connect_args -- found live, the hard way: Neon's
# pooled ("-pooler") endpoint rejects "-c statement_timeout=..." as an unsupported startup
# parameter outright ("unsupported startup parameter in options: statement_timeout"),
# which crashed the app on every startup ("Application startup failed. Exiting.") the
# moment this was first added, silently, since Render kept the last-good process alive
# through the failed deploys rather than surfacing it immediately. Setting it via a
# post-connect SET command instead (below) works fine through the pooler, since that's a
# normal query, not a startup packet field.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
)


@event.listens_for(engine, "connect")
def _set_statement_timeout(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("SET statement_timeout = 15000")
    cursor.close()


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
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                type VARCHAR NOT NULL,
                severity VARCHAR DEFAULT 'info',
                title VARCHAR NOT NULL,
                message TEXT,
                batch_id INTEGER,
                run_id INTEGER,
                read_at TIMESTAMP,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_notifications_tenant_created ON notifications (tenant_id, created_at DESC)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_conversations (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                title VARCHAR,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES chat_conversations(id),
                role VARCHAR NOT NULL,
                content TEXT NOT NULL,
                tools_used JSON,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_conversations_tenant_updated ON chat_conversations (tenant_id, updated_at DESC)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_messages_conversation_created ON chat_messages (conversation_id, created_at)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS daily_reviews (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                review_date VARCHAR NOT NULL,
                status VARCHAR DEFAULT 'pending',
                updated_at TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS review_comments (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                review_date VARCHAR NOT NULL,
                comment TEXT NOT NULL,
                created_at TIMESTAMP
            )
        """))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_daily_reviews_tenant_date ON daily_reviews (tenant_id, review_date)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_review_comments_tenant_date ON review_comments (tenant_id, review_date)"))
