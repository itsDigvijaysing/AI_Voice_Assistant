"""Shared shell execution + destructive-command denylist (AI_Linux).

Single source of truth used by BOTH ``mcp.shell.run_command`` and the typed
``mcp.skills_actions.*`` tools, so every command — however it was produced — runs through the
same catastrophic-command denylist and the same subprocess wrapper. This module has NO FastMCP /
logging side effects so it is safe to import from any server subprocess.

SAFETY: this denylist is a conservative BACKSTOP (catastrophic top-level targets only, near-zero
false positives), NOT a sandbox. The action gate (glados.core.tool_safety) is the real boundary.
"""

from __future__ import annotations

import os
import re
import subprocess

_MAX_OUTPUT = 8000  # chars of stdout/stderr returned to the model
_COMMAND_TIMEOUT = 20.0  # fixed; kept under the 30s MCP tool_timeout so the child self-kills first

_HOME = os.path.expanduser("~")
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:?\s*&\s*\}\s*;\s*:")
_DENY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"--no-preserve-root"), "rm --no-preserve-root"),
    # dd/truncate/redirect to a raw disk device (of= tolerates whitespace around '=')
    (re.compile(r"\bdd\b[^|;&\n]*\bof\s*=\s*/dev/(sd|nvme|mmcblk|vd|hd|disk)", re.I), "dd to a raw disk device"),
    (re.compile(r"\bmkfs(\.\w+)?\b[^|;&\n]*\s/dev/", re.I), "mkfs on a device"),
    (re.compile(r"\bwipefs\b", re.I), "wipefs"),
    (re.compile(r"\bshred\b[^|;&\n]*\s/dev/", re.I), "shred a device"),
    (re.compile(r"\btruncate\b[^|;&\n]*\s/dev/(sd|nvme|mmcblk|vd|hd|disk)", re.I), "truncate a device"),
    (re.compile(r">\s*/dev/(sd|nvme|mmcblk|vd|hd)", re.I), "redirect to a raw disk device"),
    (re.compile(r"\btee\b[^|;&\n]*\s/dev/(sd|nvme|mmcblk|vd|hd)", re.I), "tee to a raw disk device"),
    # recursive chmod/chown of / — flag BEFORE or AFTER the mode/owner
    (re.compile(r"\bchmod\b\s+-\S*[Rr]\S*\s+(777|000)\s+/(\s|$)"), "recursive chmod of /"),
    (re.compile(r"\bchmod\b\s+(777|000)\s+-\S*[Rr]\S*\s+/(\s|$)"), "recursive chmod of /"),
    (re.compile(r"\bchown\b\s+-\S*[Rr]\S*\s+\S+\s+/(\s|$)"), "recursive chown of /"),
    (re.compile(r"\bchown\b\s+\S+\s+-\S*[Rr]\S*\s+/(\s|$)"), "recursive chown of /"),
    # find <top-level-root> ... -delete (only a bare root target; subfolders like ~/Downloads stay allowed)
    (
        re.compile(
            rf"\bfind\b\s+[\"']?(?:/|~/?|\$HOME/?|\$\{{HOME\}}/?|/home/?|{re.escape(_HOME)}/?)[\"']?\s"
            rf"[^|;&\n]*-delete\b",
            re.I,
        ),
        "find -delete of a top-level path",
    ),
    (re.compile(r"\b(curl|wget)\b[^|;&\n]*\|\s*(sudo\s+)?(sh|bash|zsh|dash)\b", re.I), "pipe a download into a shell"),
    (_FORK_BOMB, "fork bomb"),
]

# Top-level roots: a recursive force-delete OR a `cd` target hitting one of these is catastrophic.
# A `cd` into anything NOT in this set is treated as scoping (so `cd ~/proj && rm -rf *` stays allowed).
_ROOT_TARGETS = frozenset(
    {"", "/", "~", "~/", "$HOME", "$HOME/", "${HOME}", "${HOME}/", "/home", "/home/", _HOME, _HOME + "/"}
)


def _is_root_target(target: str) -> bool:
    """True if a `cd` target is a top-level root (so cd-ing into it does NOT scope a bare-* delete).

    Bare `cd` (empty target) goes to $HOME, which is also a root.
    """
    return target.strip().strip('"').strip("'") in _ROOT_TARGETS


def _destructive_reason(command: str) -> str | None:
    """Return a reason string if the command matches a catastrophic pattern, else None."""
    c = " ".join(command.split())  # normalize whitespace
    # Also scan a quote-stripped copy: a quoted device path (dd of="/dev/sda") slips past the _DENY
    # patterns, which anchor on an unquoted /dev/. Raw disks only, so no new false positives.
    c_unquoted = c.replace('"', " ").replace("'", " ")
    for pattern, reason in _DENY:
        if pattern.search(c) or pattern.search(c_unquoted):
            return reason
    # rm -rf of a top-level target (/, ~, $HOME, /home, ~user) — NOT a subfolder like ~/Downloads. Every
    # rm in a chain is checked; long flags (--recursive/--force) are normalized to short ones first.
    home = re.escape(_HOME)
    bound = r"(?:\s|$|\))"  # token ends at whitespace, end, or a subshell ')' — so '(rm -rf /)' is caught
    roots = (
        rf"(?:^|\s)(?:/|~/|~\w+|~|\$HOME/|\$HOME|\$\{{HOME\}}/|\$\{{HOME\}}|"
        rf"/home/|/home|{home}/|{home}){bound}"
    )
    # top-level glob wipes: <root>/* and the dotfile form <root>/.* — for every way of writing the root
    glob_roots = rf"(?:/|~/|\$HOME/|\$\{{HOME\}}/|/home/|{home}/)"
    globs = rf"(?:^|\s){glob_roots}(?:\*|\.\*){bound}"
    for m in re.finditer(r"\brm\b", c):
        rm_args = re.split(r"[;&|]", c[m.end() :])[0]  # only THIS rm's args (stop at a command separator)
        norm = re.sub(r"--recursive(?:=\S*)?", " -r ", rm_args)  # long flags -> short so findall sees them
        norm = re.sub(r"--force(?:=\S*)?", " -f ", norm)
        flags = "".join(re.findall(r"(?:^|\s)-([A-Za-z]+)", norm)).lower()
        if "r" not in flags or "f" not in flags:
            continue
        scan = rm_args.replace('"', " ").replace("'", " ")  # strip quotes so the boundary regex still matches
        if re.search(roots, scan) or re.search(globs, scan):
            return "recursive force-delete of a top-level path"
        # bare *, ., ./ wipe the cwd, which is the user's home (a root). Allowed only when a `cd` into a
        # NON-root subfolder precedes this rm; a bare `cd` or `cd <root>` does not scope it.
        if re.search(rf"(?:^|\s)(?:\*|\.|\./){bound}", scan):
            cds = re.findall(r"\bcd\b\s*([^\s;&|]*)", c[: m.start()])  # governing cd = the last one
            if not cds or _is_root_target(cds[-1]):
                return "recursive force-delete of the home directory contents"
    return None


# Kept in ONE place so the probe validates exactly the props run_shell uses — a systemd that accepts
# scopes but rejects a property would otherwise pass the probe yet fail every command at spawn.
_SCOPE_CAP_PROPS: tuple[str, ...] = ("-p", "MemoryMax=2G", "-p", "TasksMax=512")

_SD_RUN_OK: bool | None = None  # lazily-probed: can we open systemd --user transient scopes with our caps?


def _systemd_run_available() -> bool:
    global _SD_RUN_OK
    if _SD_RUN_OK is None:
        try:
            probe = subprocess.run(
                ["systemd-run", "--user", "--scope", "-q", "--collect",
                 *_SCOPE_CAP_PROPS, "-p", "RuntimeMaxSec=10", "true"],
                capture_output=True, timeout=5,
            )
            _SD_RUN_OK = probe.returncode == 0
        except Exception:  # noqa: BLE001 - no systemd-run / no user manager -> plain subprocess
            _SD_RUN_OK = False
    return _SD_RUN_OK


def run_shell(command: str, timeout: float = _COMMAND_TIMEOUT, resource_caps: bool = True) -> dict:
    """Run a shell command (gated by the denylist) and return a JSON-able result dict.

    With resource_caps (default) the command runs in a transient ``systemd-run --user --scope``
    with TasksMax/MemoryMax/RuntimeMaxSec — kernel-enforced containment of fork bombs and runaway
    memory that the regex denylist can't pattern-match (see SECURITY.md: a full OS sandbox is
    non-load-bearing here, resource caps are not). Falls back to a plain subprocess when scopes
    are unavailable. Pass resource_caps=False for detached GUI launches — the launched app would
    otherwise stay in the capped scope for its whole lifetime.

    Returns {returncode, stdout, stderr} on success, or {error: ...} for empty/refused/timeout/spawn
    failure. stdout/stderr are truncated to the last _MAX_OUTPUT chars.
    """
    command = (command or "").strip()
    if not command:
        return {"error": "empty command"}
    reason = _destructive_reason(command)
    if reason is not None:
        return {"error": f"refused: destructive command blocked by the safety denylist ({reason})", "refused": reason}
    # errors="replace": a command with BINARY stdout (e.g. wl-paste when the clipboard holds an
    # image) must not raise UnicodeDecodeError and surface as a cryptic "failed to run command".
    try:
        if resource_caps and _systemd_run_available():
            # RuntimeMaxSec backstops the scope itself: if our timeout kill only reaches
            # systemd-run, the scope's survivors still die a moment later.
            argv = [
                "systemd-run", "--user", "--scope", "-q", "--collect",
                *_SCOPE_CAP_PROPS, "-p", f"RuntimeMaxSec={int(timeout) + 2}",
                "/bin/sh", "-c", command,
            ]
            proc = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, cwd=_HOME)
        else:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True, errors="replace", timeout=timeout, cwd=_HOME
            )
    except subprocess.TimeoutExpired:
        return {"error": f"command timed out after {timeout}s"}
    except Exception as exc:  # noqa: BLE001 - report any spawn failure to the model
        return {"error": f"failed to run command: {exc}"}
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-_MAX_OUTPUT:],
        "stderr": proc.stderr[-_MAX_OUTPUT:],
    }
