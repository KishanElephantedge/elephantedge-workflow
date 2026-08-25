"""Real SMTP email sending -- 2026-08-25, explicit instruction: email sends bypass Smartlead
entirely and go directly via SMTP from a specific, real mailbox (majjiinspires@gmail.com),
credentials given by the user, app password to follow separately. Standard library smtplib only
-- no new provider client/dependency.

Two required Credential rows (same Credential model/CRUD every other provider credential already
uses -- app/routes/api.py's existing /credentials routes, no new mechanism):
    smtp_email          -- the real sending mailbox address
    smtp_app_password   -- a Gmail App Password (NOT the account password -- Gmail requires this
                            for SMTP auth when 2FA is enabled, which it must be to generate one)

Gmail's real SMTP endpoint (smtp.gmail.com:587, STARTTLS) is hardcoded since the sending mailbox
is a fixed, known Gmail address, not a configurable provider -- if a non-Gmail mailbox is ever
used instead, this would need a real host/port to be added to the credential set at that point,
not guessed now."""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class SmtpError(Exception):
    pass


def send_email(sender_email: str, app_password: str, to_email: str, subject: str, body: str, to_name: str | None = None) -> None:
    """Sends ONE real email via Gmail SMTP (STARTTLS). Raises SmtpError on any failure -- the
    caller (send_via_smtp in app/gtm_os/send/channels.py) is responsible for classifying
    retryable vs. not. No retry logic here -- this is a single, real attempt only."""
    message = MIMEMultipart()
    message["From"] = sender_email
    message["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(sender_email, app_password)
            server.sendmail(sender_email, [to_email], message.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise SmtpError(f"SMTP authentication failed (check smtp_email/smtp_app_password credentials): {e}")
    except smtplib.SMTPException as e:
        raise SmtpError(f"SMTP send failed: {e}")
    except OSError as e:
        raise SmtpError(f"SMTP connection failed: {e}")
