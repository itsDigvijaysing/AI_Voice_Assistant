"""Append-only outcome log for action tool calls (AI_Linux self-improvement).

Records what the assistant tried and whether it worked (shell returncode), so retrieval can rank by
success and /tidy can flag failing skills. Best-effort: never raises into the executor. Stored in
<repo>/data/skills_feedback.jsonl (gitignored).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_DIR_READY = False  # ensure+chmod the data dir once per process (record() is on the per-tool-call hot path)


def _log_path() -> Path:
    global _DIR_READY
    data = Path(__file__).resolve().parents[2].parent / "data"  # <repo>/data
    if not _DIR_READY:
        data.mkdir(parents=True, exist_ok=True)
        try:
            # mkdir(mode=0o700) is a no-op when data/ already exists (it's git-tracked with committed
            # assets), so the intended user-only perm was never applied — enforce it here. A 0700 dir
            # blocks other users from traversing in to read the outcome log (records commands the
            # assistant ran) regardless of the file's own mode.
            data.chmod(0o700)
        except OSError:
            pass
        _DIR_READY = True
    return data / "skills_feedback.jsonl"


def shell_outcome(result: str) -> tuple[bool, int | None]:
    """Parse a mcp.shell.run_command JSON result -> (success, returncode); unparseable -> (False, None).

    An unparseable result is treated as NOT-success (don't assert a success we can't verify), so
    success ranking and /tidy never count unknown output as a win.
    """
    try:
        data = json.loads(result)
        rc = data.get("returncode")
        return (rc == 0, rc)
    except Exception:  # noqa: BLE001
        return (False, None)


def record(tool: str, args: dict | None, ok: bool, returncode: int | None = None, detail: str = "") -> None:
    """Append one outcome line. Best-effort; swallows all errors."""
    try:
        entry = {
            "ts": round(time.time(), 1),
            "tool": tool,
            "ok": bool(ok),
            "command": str((args or {}).get("command", ""))[:200],
            "returncode": returncode,
            "detail": (detail or "")[:200],
        }
        with _LOCK, _log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def recent(limit: int = 200) -> list[dict]:
    """Return the most recent outcome entries (for /tidy and retrieval ranking)."""
    path = _log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001 - skip one corrupt/partial line, keep the rest of the history
            continue
    return out
