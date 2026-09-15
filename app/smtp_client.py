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
import socket
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class _Ipv4OnlySMTP(smtplib.SMTP):
    """Forces the IPv4 address family for the connection.

    Found live (2026-09-15, first real send attempt): plain smtplib.SMTP(SMTP_HOST, SMTP_PORT)
    failed with "[Errno 101] Network is unreachable" on Render -- the container's DNS resolver
    returns an IPv6 (AAAA) address for smtp.gmail.com as its first/only usable result, but
    Render's network has no outbound IPv6 route, so the connection attempt fails before ever
    reaching Gmail. socket.create_connection() (what the base class uses) does not let a caller
    pin the address family, only the source_address -- so this overrides _get_socket() to
    resolve via getaddrinfo(..., socket.AF_INET) explicitly, the standard fix for this exact
    class of container-networking bug."""

    def _get_socket(self, host, port, timeout):
        # Same shape as the real smtplib.SMTP._get_socket (confirmed via inspect.getsource),
        # just resolving via AF_INET explicitly instead of letting create_connection() pick
        # whatever getaddrinfo() returns first.
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        addr_info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        return socket.create_connection(addr_info[0][4], timeout, self.source_address)


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
        with _Ipv4OnlySMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(sender_email, app_password)
            server.sendmail(sender_email, [to_email], message.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise SmtpError(f"SMTP authentication failed (check smtp_email/smtp_app_password credentials): {e}")
    except smtplib.SMTPException as e:
        raise SmtpError(f"SMTP send failed: {e}")
    except OSError as e:
        raise SmtpError(f"SMTP connection failed: {e}")
