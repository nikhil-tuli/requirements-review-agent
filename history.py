"""
Run history for the Requirements Review app.

One record per review. A record starts when the deep agent runs and gains a
rubric check and/or a verification loop result if the user runs them on the
same ticket text. The newest 30 records are kept.

Where records live:
  - Neon (hosted Postgres) when NEON_DATABASE_URL is set
  - runs/history.json on this machine otherwise, or if Neon cannot be reached

Both stores have the same methods: add, get, update, list, prune.
"""

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_RUNS = 30

STEP_LABELS = {"agent": "Agent", "check": "Rubric check", "verify": "Verified"}


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Local file store
# ---------------------------------------------------------------------------

class LocalStore:
    backend = "local"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.note = ""
        self._lock = threading.Lock()

    def describe(self) -> str:
        return f"this machine ({self.path.name})"

    def _read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _write(self, rows: List[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=2))
        tmp.replace(self.path)

    def add(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            rows = self._read()
            rows.append(rec)
            self._write(rows)

    def get(self, rec_id: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self._read() if r["id"] == rec_id), None)

    def update(self, rec_id: str, rec: Dict[str, Any]) -> None:
        with self._lock:
            rows = self._read()
            rows = [rec if r["id"] == rec_id else r for r in rows]
            self._write(rows)

    def list(self, limit: int = MAX_RUNS) -> List[Dict[str, Any]]:
        rows = sorted(self._read(), key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

    def prune(self, keep: int = MAX_RUNS) -> None:
        with self._lock:
            rows = sorted(self._read(), key=lambda r: r["created_at"], reverse=True)[:keep]
            self._write(rows)


# ---------------------------------------------------------------------------
# Neon (Postgres) store
# ---------------------------------------------------------------------------

class NeonStore:
    backend = "neon"

    def __init__(self, url: str):
        self.url = url
        self.note = ""
        self._ensure_table()

    def describe(self) -> str:
        return "Neon (hosted database)"

    def _connect(self):
        import psycopg
        last = None
        for _ in range(2):  # a sleeping Neon database can need a second try
            try:
                return psycopg.connect(self.url, connect_timeout=15, autocommit=True)
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1)
        raise last

    def _ensure_table(self) -> None:
        with self._connect() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS review_runs ("
                " id TEXT PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL,"
                " ticket_id TEXT,"
                " data JSONB NOT NULL)"
            )

    def add(self, rec: Dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb
        with self._connect() as c:
            c.execute(
                "INSERT INTO review_runs (id, created_at, ticket_id, data) VALUES (%s, %s, %s, %s)",
                (rec["id"], rec["created_at"], rec.get("ticket_id"), Jsonb(rec)),
            )

    def get(self, rec_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as c:
            row = c.execute("SELECT data FROM review_runs WHERE id = %s", (rec_id,)).fetchone()
        return row[0] if row else None

    def update(self, rec_id: str, rec: Dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb
        with self._connect() as c:
            c.execute("UPDATE review_runs SET data = %s WHERE id = %s", (Jsonb(rec), rec_id))

    def list(self, limit: int = MAX_RUNS) -> List[Dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT data FROM review_runs ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [r[0] for r in rows]

    def prune(self, keep: int = MAX_RUNS) -> None:
        with self._connect() as c:
            c.execute(
                "DELETE FROM review_runs WHERE id NOT IN "
                "(SELECT id FROM review_runs ORDER BY created_at DESC LIMIT %s)",
                (keep,),
            )


def get_store(local_path: Path):
    """Neon if configured and reachable, otherwise the local file."""
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url:
        try:
            return NeonStore(url)
        except Exception as e:  # noqa: BLE001
            store = LocalStore(local_path)
            store.note = f"Could not reach Neon ({type(e).__name__}). Using a local file instead."
            return store
    return LocalStore(local_path)


# ---------------------------------------------------------------------------
# Building and reading records
# ---------------------------------------------------------------------------

def _total_tokens(rec: Dict[str, Any]) -> int:
    total = 0
    for key in ("agent", "check", "verify"):
        part = rec.get(key) or {}
        total += int((part.get("usage") or {}).get("total_tokens", 0) or 0)
    return total


def new_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def base_record(ticket_id: str, ticket_text: str, model: str, agents_hash: str, rubric_hash: str) -> Dict[str, Any]:
    return {
        "id": new_id(), "created_at": _now_iso(), "ticket_id": ticket_id,
        "ticket_text": ticket_text, "ticket_hash": text_hash(ticket_text),
        "model": model, "agents_md_hash": agents_hash, "rubric_hash": rubric_hash,
        "kinds": [],
    }


def _add_kind(rec: Dict[str, Any], kind: str) -> None:
    if kind not in rec["kinds"]:
        rec["kinds"].append(kind)


def attach_agent(rec: Dict[str, Any], res: Dict[str, Any]) -> None:
    run = res["run"]
    rec["agent"] = {
        "report": run.analysis_markdown, "turns": run.turns, "counts": run.counts,
        "usage": res["usage"], "archive": res.get("archive", ""),
        "trace": [{"step": s.step, "actor": s.actor, "action": s.action, "detail": s.detail}
                  for s in run.trace],
    }
    _add_kind(rec, "agent")
    rec["tokens_total"] = _total_tokens(rec)


def attach_check(rec: Dict[str, Any], res: Dict[str, Any]) -> None:
    rec["check"] = {"criteria": res["criteria"], "all_passed": res["all_passed"],
                    "usage": res["usage"], "archive": res.get("archive", "")}
    _add_kind(rec, "check")
    rec["tokens_total"] = _total_tokens(rec)


def attach_verify(rec: Dict[str, Any], res: Dict[str, Any]) -> None:
    run = res["run"]
    rec["verify"] = {
        "report": run.analysis_markdown, "status": run.status, "turns": run.turns,
        "grading": [{"iteration": g.iteration, "result": g.result, "criteria": g.criteria,
                     "analysis_markdown": g.analysis_markdown} for g in run.grading],
        "usage": res["usage"], "archive": res.get("archive", ""),
    }
    _add_kind(rec, "verify")
    rec["tokens_total"] = _total_tokens(rec)


def result_report(rec: Dict[str, Any]) -> str:
    """The verified report if there is one, otherwise the deep agent report."""
    if rec.get("verify") and rec["verify"].get("report"):
        return rec["verify"]["report"]
    return (rec.get("agent") or {}).get("report", "")


def rubric_result(rec: Dict[str, Any]) -> str:
    """'2 of 3 pass' from the final verification pass, else from the rubric check."""
    criteria: List[Dict[str, Any]] = []
    grading = (rec.get("verify") or {}).get("grading") or []
    if grading:
        criteria = grading[-1].get("criteria") or []
    elif rec.get("check"):
        criteria = rec["check"].get("criteria") or []
    if not criteria:
        return "not graded"
    passed = sum(1 for c in criteria if c.get("passed"))
    return f"{passed} of {len(criteria)} pass"


def steps_label(rec: Dict[str, Any]) -> str:
    return ", ".join(STEP_LABELS[k] for k in rec.get("kinds", []) if k in STEP_LABELS)


def summary_rows(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for r in records:
        when = r["created_at"][:16].replace("T", " ") + " UTC"
        rows.append({"Time": when, "Ticket": r.get("ticket_id", ""), "Steps": steps_label(r),
                     "Rubric": rubric_result(r), "Tokens": int(r.get("tokens_total", 0) or 0),
                     "Model": r.get("model", "")})
    return rows
