import json
import os
import subprocess

from app.config import settings


class DeeplineError(Exception):
    pass


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
    result = subprocess.run(
        [settings.deepline_cli_path, "tools", "execute", tool_id, "--input", json.dumps(payload), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
    )
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
