import json
import os
import signal
import subprocess

from app.config import settings


class DeeplineError(Exception):
    pass


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
    key = os.environ.get("DEEPLINE_API_KEY", "")
    host = os.environ.get("DEEPLINE_HOST_URL", "")
    key_view = f"len={len(key)} repr_ends={key[-6:]!r}" if key else "UNSET"
    return f"DEEPLINE_API_KEY[{key_view}] DEEPLINE_HOST_URL={host!r}"


def execute_tool(tool_id: str, payload: dict) -> dict:
    """Run `deepline tools execute <tool_id> --input '<json>' --json` and return the parsed response."""
    try:
        result = _run_deepline_cli(
            [settings.deepline_cli_path, "tools", "execute", tool_id, "--input", json.dumps(payload), "--json"],
            timeout_seconds=120,
        )
    except subprocess.TimeoutExpired as e:
        raise DeeplineError(f"{tool_id} timed out after 120s") from e
    if result.returncode != 0:
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
    real calls fast and correct in ~3s, 1 of 3 hung the full timeout) -- one retry on failure
    smooths over that without masking a persistent problem, since BudgetGuard's caller still
    sees a real DeeplineError (and fails the run safely, per its own docstring) if both
    attempts fail."""
    try:
        return _get_credit_balance_usd_once(timeout_seconds=20)
    except DeeplineError:
        return _get_credit_balance_usd_once(timeout_seconds=20)


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
