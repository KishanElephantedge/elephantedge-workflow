import json
import logging
import os
import re
import signal
import subprocess
import time

from app.config import settings

logger = logging.getLogger(__name__)


class DeeplineError(Exception):
    pass



# Deepline was the ONLY provider in this codebase reading its key from the process environment.
# Apify, Jobo, Claude, Gemini, HubSpot, SalesRobot and Smartlead all read theirs from the
# Credential table. That inconsistency is what keeps breaking: a key that works locally can be
# rejected in production with AUTH_ERROR/401, and there is no way to see or change what the
# container actually received without a redeploy. It cost a full day of discovery on 2026-09-01
# and again on 2026-09-03, and a previous incident needed a temporary diagnostic route deployed
# just to inspect the value.
#
# The key now comes from the Credential table first, exactly like every other provider, and is
# passed EXPLICITLY into the CLI subprocess rather than inherited -- so what the CLI receives is
# what the database holds, not whatever survived the deploy. The env var stays as a fallback so
# nothing breaks if the row is missing.
#
# Whitespace is stripped: a key pasted into a Render env field can carry a trailing newline, which
# is invisible in the dashboard and produces exactly the same 401 as a genuinely wrong key.
_KEY_CACHE: dict[str, str] = {}


def _resolve_api_key() -> str:
    if "key" in _KEY_CACHE:
        return _KEY_CACHE["key"]
    key = ""
    try:
        from app.db.models import Credential
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            row = (
                db.query(Credential)
                .filter(Credential.name == "deepline_api_key", Credential.value.isnot(None))
                .order_by(Credential.id.desc())
                .first()
            )
            key = (row.value or "").strip() if row else ""
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 -- a DB hiccup must fall back to env, not break every call
        logger.warning("deepline: could not read key from Credential table (%s), falling back to env", e)
    if not key:
        key = os.environ.get("DEEPLINE_API_KEY", "").strip()
    _KEY_CACHE["key"] = key
    return key


def _cli_env() -> dict:
    """Environment for the CLI subprocess, with the resolved key forced in."""
    env = dict(os.environ)
    key = _resolve_api_key()
    if key:
        env["DEEPLINE_API_KEY"] = key
    return env


def _run_deepline_cli(args: list[str], timeout_seconds: float) -> subprocess.CompletedProcess:
    """subprocess.run(timeout=...), but killing the WHOLE process group on timeout, not
    just the immediate child. Found live: a stuck run with 0 progress for 6+ minutes,
    with the app process itself still healthy -- subprocess.run's own timeout only
    signals the direct child (the `deepline` Node process); if that process has itself
    spawned children holding the stdout/stderr pipes open, killing just the parent
    leaves those pipes open and communicate() hangs indefinitely waiting for EOF,
    completely bypassing the timeout. start_new_session=True puts the child (and
    anything it spawns) in its own process group, so os.killpg can take the entire
    tree down on timeout."""
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=_cli_env(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()  # reap now that the whole group is dead
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def _masked_env_debug() -> str:
    """Masked view of what this process actually sees for the two Deepline env vars --
    diagnostic only, for a live 401 that a known-good key doesn't reproduce locally
    (suggests the value reaching this container differs from the intended one, e.g. a
    stray whitespace/newline from how it was pasted into Render)."""
    key = _resolve_api_key()
    env_key = os.environ.get("DEEPLINE_API_KEY", "")
    host = os.environ.get("DEEPLINE_HOST_URL", "")
    key_view = f"len={len(key)} ends={key[-6:]!r}" if key else "UNSET"
    src = "credential_table" if key and key != env_key.strip() else ("env" if key else "none")
    return f"DEEPLINE_API_KEY[{key_view} source={src}] DEEPLINE_HOST_URL={host!r}"


RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_DEFAULT_DELAY_SECONDS = 5


def _rate_limit_retry_delay(stdout: str) -> float | None:
    """Returns the delay to wait before retrying, if stdout is a rate-limit error response --
    found live: both autonomous days in a row failed the entire run outright on a single
    transient 429 from crustdata_v3_person_search, whose own error body literally said "Retry
    after 5000ms" -- there was no retry at all, just an instant hard failure. Real spend from
    each failure was tiny (the rate-limited call itself isn't even billed), but the functional
    cost was two full wasted days. Parses the provider's own suggested delay when present."""
    if '"code": "RATE_LIMIT"' not in stdout and '"code":"RATE_LIMIT"' not in stdout:
        return None
    match = re.search(r"[Rr]etry after (\d+)ms", stdout)
    return int(match.group(1)) / 1000 if match else RATE_LIMIT_DEFAULT_DELAY_SECONDS


def execute_tool(tool_id: str, payload: dict) -> dict:
    """Run `deepline tools execute <tool_id> --input '<json>' --json` and return the parsed
    response. Retries on a rate-limit response (see _rate_limit_retry_delay) before giving up
    -- any other failure still raises immediately, no change there."""
    result = None
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        try:
            result = _run_deepline_cli(
                [settings.deepline_cli_path, "tools", "execute", tool_id, "--input", json.dumps(payload), "--json"],
                timeout_seconds=120,
            )
        except subprocess.TimeoutExpired as e:
            raise DeeplineError(f"{tool_id} timed out after 120s") from e
        except FileNotFoundError as e:
            # Found live: a real production run failed outright with "[Errno 2] No such
            # file or directory: 'deepline'" -- correlated against deploy timing, this
            # happened seconds after a git push, almost certainly a container mid-deploy-
            # rollover where the CLI binary (baked into the image at build time, see
            # Dockerfile) briefly wasn't in place yet. The very next run, a minute later,
            # worked normally -- confirming it was transient, not a persistent break.
            # Retrying with a short delay lets this kind of deploy race self-heal instead
            # of failing the whole day's run outright.
            if attempt < RATE_LIMIT_MAX_RETRIES:
                time.sleep(RATE_LIMIT_DEFAULT_DELAY_SECONDS)
                continue
            raise DeeplineError(f"{tool_id}: deepline CLI not found after {RATE_LIMIT_MAX_RETRIES} retries: {e}") from e
        if result.returncode == 0:
            break
        delay = _rate_limit_retry_delay(result.stdout)
        if delay is not None and attempt < RATE_LIMIT_MAX_RETRIES:
            time.sleep(delay)
            continue
        raise DeeplineError(
            f"{tool_id} failed (exit {result.returncode}): stdout={result.stdout!r} stderr={result.stderr!r} "
            f"env={_masked_env_debug()}"
        )
    stdout = result.stdout
    brace_index = stdout.find("{")
    if brace_index == -1:
        raise DeeplineError(f"{tool_id} returned no JSON output: {stdout[:500]}")
    try:
        return json.loads(stdout[brace_index:])
    except json.JSONDecodeError as e:
        raise DeeplineError(f"{tool_id} returned malformed JSON: {stdout[:500]}") from e


def _get_credit_balance_usd_once(timeout_seconds: float) -> float:
    try:
        result = _run_deepline_cli(
            [settings.deepline_cli_path, "billing", "balance", "--json"],
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        raise DeeplineError(f"billing balance timed out after {timeout_seconds}s") from e
    if result.returncode != 0:
        raise DeeplineError(f"billing balance failed: {result.stderr or result.stdout}")
    stdout = result.stdout
    brace_index = stdout.find("{")
    if brace_index == -1:
        raise DeeplineError(f"billing balance returned no JSON output: {stdout[:500]}")
    data = json.loads(stdout[brace_index:])
    return float(data["rough_usd_balance"])


def get_credit_balance_usd() -> float:
    """Current Deepline account balance in USD, used to enforce the daily spend cap -- same
    pattern as Synefi's version, checked before/after paid phases rather than trusting a
    per-call cost estimate.

    This call has shown itself intermittently slow/hanging in production (observed: 2 of 3
    real calls fast and correct in ~3s, 1 of 3 hung the full timeout) -- retries smooth over
    that without masking a persistent problem, since BudgetGuard's caller still sees a real
    DeeplineError (and fails the run safely, per its own docstring) if every attempt fails.

    Two attempts were not enough. On 2026-09-01 both timed out, BudgetGuard could not be
    constructed, and the entire discovery stage failed -- so the day produced no new companies,
    the run re-drafted to contacts it had already messaged, and the 30-day cooldown then blocked
    almost every send. One flaky subprocess cost a full day of outreach.

    The guard itself is NOT skippable: run_discovery spends real Deepline credits through
    crustdata_companydb_search, so proceeding without a verified balance would be unguarded paid
    spend. The fix is therefore to make the check succeed, not to bypass it. A local run measured
    4-5s, so a 45s ceiling is far beyond a healthy call -- it only ever costs time on the hang
    case, which is the case worth waiting through.
    """
    last_error: DeeplineError | None = None
    for timeout_seconds in (20, 30, 45):
        try:
            return _get_credit_balance_usd_once(timeout_seconds=timeout_seconds)
        except DeeplineError as e:
            last_error = e
            logger.warning("deepline billing balance failed at %ss timeout: %s", timeout_seconds, e)
    raise last_error if last_error else DeeplineError("billing balance failed with no recorded error")


def extract_rows(response: dict, *keys: str) -> list[dict]:
    """Pull the row list out of a tool response. Deepline tools are inconsistent about
    shape: `raw` is sometimes the list itself, sometimes a dict with the list under a
    tool-specific key. Try each candidate key in order, but only after confirming raw
    isn't already the list."""
    raw = response.get("toolResponse", {}).get("raw", {})
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if isinstance(value, list):
                return value
    return []
