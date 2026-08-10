"""Free, self-built email verification via an SMTP RCPT TO handshake -- connects to the
domain's real mail server and asks whether it would accept mail to a given address, without
actually sending anything. Confirmed live (2026-08-10) against known-real and known-fake
addresses across Outlook/Google Workspace/Mimecast-hosted domains: correctly distinguishes
real recipients (250) from non-existent ones (550), and catch-all domains are detected by
probing a random nonexistent address first.

NOT CURRENTLY WIRED IN -- confirmed live (2026-08-10) that Render blocks all outbound port
25 at the network level ("[Errno 101] Network is unreachable"), so this cannot run from our
production host as-is. Works perfectly from an unrestricted network (tested locally). Left
in place for if/when we run this from a host that allows outbound SMTP (a small VM, a
different PaaS, or a proxy service), rather than deleting proven-working code."""
import smtplib
import socket

import dns.resolver

PROBE_LOCAL_PART = "definitely-not-a-real-person-xyz999"


class EmailVerifyError(Exception):
    pass


def _get_mx_hosts(domain: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=10)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.Timeout):
        return []
    return [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]


def _rcpt_check(mx_host: str, email: str, from_addr: str) -> int:
    server = smtplib.SMTP(timeout=10)
    try:
        server.connect(mx_host, 25)
        server.helo("elephantedge.ai")
        server.mail(from_addr)
        code, _ = server.rcpt(email)
        return code
    finally:
        try:
            server.quit()
        except Exception:
            pass


def verify_email(email: str, from_addr: str = "probe@elephantedge.ai") -> dict:
    """Returns {"deliverable": True/False/None, "reason": str}. None means inconclusive
    (no MX, connection blocked/timed out, catch-all domain) -- never treat None as a hit."""
    domain = email.split("@")[-1]
    mx_hosts = _get_mx_hosts(domain)
    if not mx_hosts:
        return {"deliverable": None, "reason": "no_mx"}

    mx = mx_hosts[0]
    try:
        catchall_code = _rcpt_check(mx, f"{PROBE_LOCAL_PART}@{domain}", from_addr)
    except (smtplib.SMTPException, socket.error, OSError) as e:
        return {"deliverable": None, "reason": f"connect_failed: {e}"}

    if catchall_code == 250:
        return {"deliverable": None, "reason": "catch_all_domain"}

    try:
        code = _rcpt_check(mx, email, from_addr)
    except (smtplib.SMTPException, socket.error, OSError) as e:
        return {"deliverable": None, "reason": f"connect_failed: {e}"}

    return {"deliverable": code == 250, "reason": f"smtp_{code}"}


def guess_and_verify(first_name: str, last_name: str, domain: str, from_addr: str = "probe@elephantedge.ai") -> str | None:
    """Tries common patterns in order, returns the first one that verifies deliverable, or
    None if nothing matches (including the catch-all/inconclusive case -- never guess blind)."""
    if not first_name or not domain:
        return None
    f = first_name.strip().lower()
    l = (last_name or "").strip().lower()
    candidates = [f"{f}@{domain}"]
    if l:
        candidates += [f"{f}.{l}@{domain}", f"{f[0]}{l}@{domain}", f"{f}{l}@{domain}", f"{l}@{domain}"]

    mx_hosts = _get_mx_hosts(domain)
    if not mx_hosts:
        return None
    mx = mx_hosts[0]
    try:
        catchall_code = _rcpt_check(mx, f"{PROBE_LOCAL_PART}@{domain}", from_addr)
    except (smtplib.SMTPException, socket.error, OSError):
        return None
    if catchall_code == 250:
        return None  # catch-all -- can't distinguish real from fake, don't guess

    for cand in candidates:
        try:
            code = _rcpt_check(mx, cand, from_addr)
        except (smtplib.SMTPException, socket.error, OSError):
            continue
        if code == 250:
            return cand
    return None
