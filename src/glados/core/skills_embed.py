"""Optional local embedding retrieval (AI_Linux Phase 3).

Semantic fallback for skills_index: when keyword retrieval finds nothing (e.g. "make the screen darker"
has no skill keyword), embed the query with a local Ollama model (nomic-embed-text, CPU-friendly) and
match by cosine. Skill vectors are cached on disk and regenerated only when skills change. Any failure
(model not pulled, Ollama down) raises so the caller falls back to keyword — retrieval never breaks.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import requests

_MODEL = os.environ.get("GLADOS_EMBED_MODEL", "nomic-embed-text")
_HOST = os.environ.get("GLADOS_OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
_mem: dict = {"sig": None, "vecs": {}}
_LOCK = threading.Lock()  # guards _mem (concurrent engine + MCP retrieval); _embed() stays outside it


def _cache_path() -> Path:
    data = Path(__file__).resolve().parents[2].parent / "data"  # <repo>/data
    data.mkdir(parents=True, exist_ok=True)
    try:  # mkdir(mode=) is ignored for the pre-existing git-tracked data/; enforce user-only here
        data.chmod(0o700)
    except OSError:
        pass
    return data / "skills_vectors.json"


def _embed(texts: list[str]) -> list[list[float]]:
    resp = requests.post(f"{_HOST}/api/embed", json={"model": _MODEL, "input": texts}, timeout=30)
    resp.raise_for_status()
    embs = resp.json().get("embeddings")
    if not embs or len(embs) != len(texts):
        raise RuntimeError("embed response malformed")
    return embs


def _cos(a: list[float], b: list[float]) -> float:
    dot = da = db = 0.0
    for x, y in zip(a, b):
        dot += x * y
        da += x * x
        db += y * y
    return dot / ((da ** 0.5) * (db ** 0.5) + 1e-9)


def _content_sig(skills: list[dict]) -> str:
    digest = hashlib.sha256()
    for sk in sorted(skills, key=lambda s: s["name"]):
        digest.update((sk["name"] + "|" + sk["trigger"] + "|" + " ".join(sk.get("commands", []))).encode())
    digest.update(_MODEL.encode())
    return digest.hexdigest()


def _skill_vectors(skills: list[dict]) -> dict[str, list[float]]:
    """Embeddings per skill, cached in memory + on disk, regenerated when skills/model change."""
    sig = _content_sig(skills)
    with _LOCK:
        if _mem["sig"] == sig:
            return _mem["vecs"]
    cache = _cache_path()
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("sig") == sig:
                with _LOCK:
                    _mem.update(sig=sig, vecs=data["vecs"])
                return data["vecs"]
        except Exception:  # noqa: BLE001
            pass
    texts = [f"{sk['name']}. {sk['trigger']}. {' '.join(sk.get('commands', []))}" for sk in skills]
    embs = _embed(texts)  # network call kept OUTSIDE the lock so it can't serialize all retrieval
    vecs = {sk["name"]: emb for sk, emb in zip(skills, embs)}
    try:
        cache.write_text(json.dumps({"sig": sig, "vecs": vecs}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    with _LOCK:
        _mem.update(sig=sig, vecs=vecs)
    return vecs


def semantic_ranked(query: str, skills: list[dict]) -> list[tuple[dict, float]]:
    """Rank skills by query↔skill cosine; only those above the threshold get a score (others 0.0)."""
    vecs = _skill_vectors(skills)
    qvec = _embed([query])[0]
    thresh = float(os.environ.get("GLADOS_EMBED_THRESHOLD", "0.6"))
    scored = [(sk, _cos(qvec, vecs[sk["name"]])) for sk in skills if sk["name"] in vecs]
    hits = sorted(((sk, c) for sk, c in scored if c >= thresh), key=lambda x: x[1], reverse=True)
    kept = {sk["name"] for sk, _ in hits}
    return hits + [(sk, 0.0) for sk in skills if sk["name"] not in kept]
