"""One-off test: does a Render Background Worker (unlike a free Web Service) allow outbound
SMTP? Render's own changelog specifically says "Free WEB SERVICES will no longer allow outbound
traffic to SMTP ports" -- a Background Worker is a different service type (no inbound HTTP,
runs continuously), and the wording suggests it may not be covered by the same restriction.

Deploy this AS a Background Worker (not a Web Service) on Render, same repo, same
DATABASE_URL as the existing backend (copy the env var across -- this script needs it to read
the real smtp_email/smtp_app_password credentials, the same way every other part of this app
does; it never receives or logs the raw password itself).

Start Command: python worker_smtp_test.py

Sends ONE real email to the configured mailbox itself (never a prospect), then logs the
result and sleeps forever so Render doesn't treat a normal exit as a crash-loop."""
import sys
import time

sys.path.insert(0, ".")

from app.db.session import SessionLocal
from app.gtm_os.send.channels import _get_smtp_credential
from app.smtp_client import SmtpError, send_email

ELEPHANT_EDGE_TENANT_ID = 2

print("worker_smtp_test: starting", flush=True)

db = SessionLocal()
try:
    sender_email = _get_smtp_credential(db, ELEPHANT_EDGE_TENANT_ID, "smtp_email")
    app_password = _get_smtp_credential(db, ELEPHANT_EDGE_TENANT_ID, "smtp_app_password")
finally:
    db.close()

if not sender_email or not app_password:
    print("worker_smtp_test: RESULT = FAIL (smtp_email/smtp_app_password not configured)", flush=True)
else:
    try:
        send_email(sender_email, app_password, sender_email, "Background Worker SMTP test",
                   "If you're reading this, a Render Background Worker CAN send real SMTP -- unlike the free Web Service.")
        print(f"worker_smtp_test: RESULT = SUCCESS (sent to {sender_email})", flush=True)
    except SmtpError as e:
        print(f"worker_smtp_test: RESULT = FAIL ({e})", flush=True)

print("worker_smtp_test: done, sleeping to keep the worker alive for log review", flush=True)
while True:
    time.sleep(3600)
