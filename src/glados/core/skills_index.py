"""Shared, in-process skills index (AI_Linux).

Single source of truth for reading skills/SKILL-*.md and ranking them for a query. Since the
native-tools pivot (305a97c) the default runtime does NOT retrieve or inject skills per turn,
desktop actions are typed mcp.skills_actions.* tools. This index now serves: the OPTIONAL
mcp.skills server (find_skill/list_skills, disabled by default), /learn's write_skill, and
/tidy's catalog. Keyword retrieval by default; the hybrid embedding path plugs in behind
top_skills() with automatic keyword fallback.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_CMD_RE = re.compile(r"`([^`\n]+)`")  # back-ticked inline commands in a SKILL body
_SKIP_PREFIX = ("command -v", "ls ", "cat ", "echo ", "which ", "sudo apt", "./ai-linux")  # checks/meta, not the action
# Query fillers dropped before matching, so "what is your name" doesn't match on stray tokens.
_STOP = {
    "the", "a", "an", "is", "am", "are", "was", "be", "to", "of", "it", "its", "in", "on", "at", "for",
    "my", "me", "you", "your", "yours", "i", "we", "can", "could", "please", "do", "does", "did", "what",
    "whats", "how", "why", "when", "who", "want", "wanna", "would", "should", "like", "this", "that",
    "these", "those", "and", "or", "but", "with", "just", "now", "tell", "give", "let", "some", "bit",
    # generic action verbs that appear in many skill triggers -> drop so the distinctive noun decides.
    "make", "set", "change", "turn", "adjust", "get", "go", "put", "use", "try",
}


def skills_dir() -> Path:
    """Resolve the skills directory: $GLADOS_SKILLS_DIR, else <repo>/skills, else ./skills."""
    env = os.environ.get("GLADOS_SKILLS_DIR")
    if env:
        return Path(env).expanduser()
    repo_skills = Path(__file__).resolve().parents[2].parent / "skills"  # <repo>/src/glados/core -> <repo>
    if repo_skills.exists():
        return repo_skills
    return Path.cwd() / "skills"


def _extract_commands(text: str) -> list[str]:
    """Pull representative action commands (back-ticked) from a SKILL body, skipping checks/installs."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _CMD_RE.findall(text):
        cmd = raw.strip()
        if not cmd or " " not in cmd or len(cmd) > 200:
            continue
        if cmd.startswith(("mcp.", "http", "$", "#")) or cmd.lower().startswith(_SKIP_PREFIX):
            continue
        if cmd not in seen:
            seen.add(cmd)
            out.append(cmd)
    return out[:6]


_cache: dict[str, object] = {"sig": None, "skills": []}


def _dir_signature(directory: Path) -> tuple:
    if not directory.exists():
        return ()
    return tuple((str(p), p.stat().st_mtime) for p in sorted(directory.rglob("SKILL-*.md")))


def load_skills() -> list[dict]:
    """Load all SKILL-*.md (incl. learned/) -> dicts; cached until a skill file changes (mtime sig)."""
    directory = skills_dir()
    sig = _dir_signature(directory)
    if _cache["sig"] == sig:
        return _cache["skills"]  # type: ignore[return-value]
    skills: list[dict] = []
    for path in sorted(directory.rglob("SKILL-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        name = path.stem
        m = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
        if m:
            name = m.group(1).strip()
        trigger = ""
        t = re.search(r"^trigger:\s*(.+)$", text, re.MULTILINE)
        if t:
            trigger = t.group(1).strip()
        skills.append(
            {"name": name, "trigger": trigger, "file": path.name, "text": text, "commands": _extract_commands(text)}
        )
    _cache["sig"] = sig
    _cache["skills"] = skills
    return skills


def _keyword_ranked(query: str, skills: list[dict]) -> list[tuple[dict, float]]:
    qwords = {w for w in re.findall(r"\w+", (query or "").lower()) if len(w) >= 2 and w not in _STOP}
    ranked: list[tuple[dict, float]] = []
    for skill in skills:
        # Whole-word match against name + trigger + commands only (NOT the raw frontmatter, and NOT
        # substrings, else "is" matches "raise" and "me" matches "volume").
        hay_words = set(re.findall(r"\w+", f"{skill['name']} {skill['trigger']} {' '.join(skill.get('commands', []))}".lower()))
        ranked.append((skill, float(sum(1 for w in qwords if w in hay_words))))
    # tie-break toward skills that carry a runnable command (so an actionable skill beats a command-less
    # one like focus-or-control-window when both match a shared word such as "screen").
    ranked.sort(key=lambda item: (item[1], 1 if item[0].get("commands") else 0), reverse=True)
    return ranked


def _ranked(query: str, skills: list[dict]) -> list[tuple[dict, float]]:
    """Keyword-first ranking; in hybrid mode, fall back to local embeddings only when keyword misses."""
    keyword = _keyword_ranked(query, skills)
    mode = os.environ.get("GLADOS_SKILLS_RETRIEVAL", "keyword").strip().lower()
    if mode != "hybrid" or (keyword and keyword[0][1] > 0):
        return keyword  # keyword default, or a clear keyword hit -> fast path (no embedding call)
    try:
        from . import skills_embed  # Phase 3: optional semantic fallback; any failure -> keyword

        semantic = skills_embed.semantic_ranked(query, skills)
        if semantic and semantic[0][1] > 0:
            return semantic
    except Exception as exc:  # noqa: BLE001
        logger.warning("skills semantic retrieval unavailable ({}); using keyword", exc)
    return keyword


def top_skills(query: str, k: int = 2) -> list[dict]:
    """Return up to k best-matching skills for the query (score>0 only)."""
    skills = load_skills()
    if not skills:
        return []
    return [skill for skill, score in _ranked(query, skills)[:k] if score > 0]


def best_skill(query: str) -> dict | None:
    """The single best skill for a query, or None (used by mcp.skills.find_skill)."""
    top = top_skills(query, k=1)
    return top[0] if top else None


def _slug(name: str) -> str:
    """Sanitize a name to [a-z0-9-] so a learned skill can only land inside skills/learned/."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:48] or "skill"


def write_skill(name: str, trigger: str = "", commands: list[str] | str | None = None, notes: str = "") -> dict:
    """Write a learned SKILL draft under skills/learned/ (markdown only; never executes). Returns dict."""
    name = (name or "").strip()
    if isinstance(commands, str):
        commands = [commands]
    cmds = [str(c).strip() for c in (commands or []) if str(c).strip()]
    if not name or not cmds:
        return {"error": "need a name and at least one command"}
    learned = skills_dir() / "learned"
    try:
        learned.mkdir(parents=True, exist_ok=True)
        path = learned / f"SKILL-{_slug(name)}.md"
        lines = [
            "---",
            f"name: {name}",
            f"trigger: {trigger.strip() or f'user asks to {name}'}",
            "tools: [mcp.shell.run_command]",
            "source: learned",
            f"created: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "---",
            "",
            f"To {name}, run one of these commands via mcp.shell.run_command:",
        ]
        lines += [f"- `{c}`" for c in cmds]
        if notes.strip():
            lines += ["", notes.strip()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not write skill: {exc}"}
    return {"ok": True, "file": str(path.relative_to(skills_dir()))}
