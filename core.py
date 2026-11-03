"""
Core functions for the Requirements Review app.

The UI (app.py) only calls the functions here, so a scheduler or webhook can
reuse them later. Everything reuses deepagent_helpers.py:
  run_review        -> agent loop
  run_check         -> grade-only check of an analysis.md against the rubric
  run_verification  -> verification loop (agent + grader + revisions)

Each call returns a dict with token usage and the folder where the run was
archived under runs/.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import get_usage_metadata_callback

import deepagent_helpers as dh

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
JIRA_JSON = BASE_DIR / "jira_tickets.json"
AGENTS_FILE = BASE_DIR / dh.WORKSPACE_DIR / "memories" / "AGENTS.md"

RUBRIC_FILE = BASE_DIR / "rubric.md"

MODEL = dh.DEFAULT_MODEL


def load_rubric() -> str:
    """The rubric lives in rubric.md; the helper's built-in default is the fallback."""
    if RUBRIC_FILE.exists() and RUBRIC_FILE.read_text().strip():
        return RUBRIC_FILE.read_text().strip()
    return dh.DEFAULT_RUBRIC


RUBRIC = load_rubric()


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def list_tickets() -> List[str]:
    return list(dh._FAKE_JIRA)


def get_ticket_text(ticket_id: str) -> str:
    return dh._FAKE_JIRA.get(ticket_id, "")


def set_ticket(ticket_id: str, text: str) -> None:
    """Session-only: override or add a ticket in the fake Jira backend."""
    dh.seed_fake_jira(ticket_id, text)


def next_custom_id() -> str:
    n = 1
    while f"CUSTOM-{n}" in dh._FAKE_JIRA:
        n += 1
    return f"CUSTOM-{n}"


def save_tickets_to_json() -> Path:
    """Write the current tickets (including edits and new ones) to the JSON file."""
    JIRA_JSON.write_text(json.dumps(dh._FAKE_JIRA, indent=2, ensure_ascii=False) + "\n")
    return JIRA_JSON


def read_agents_md() -> str:
    return AGENTS_FILE.read_text() if AGENTS_FILE.exists() else "(AGENTS.md not found)"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def check_api_key() -> None:
    if MODEL.startswith("groq:") and not os.environ.get("GROQ_API_KEY"):
        raise RuntimeError("GROQ_API_KEY is missing. Add it to your .env file and restart the app.")


def _usage(cb) -> Dict[str, Any]:
    """Sum token usage across every model the callback saw."""
    by_model = {}
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for model, u in (cb.usage_metadata or {}).items():
        row = {k: int(u.get(k, 0) or 0) for k in totals}
        by_model[model] = row
        for k in totals:
            totals[k] += row[k]
    return {**totals, "by_model": by_model}


def usage_line(usage: Dict[str, Any]) -> str:
    if not usage or not usage.get("total_tokens"):
        return "Tokens: not reported by the provider for this run."
    return (f"Tokens: {usage['input_tokens']:,} input, "
            f"{usage['output_tokens']:,} output, {usage['total_tokens']:,} total")


def _archive_dir(ticket_id: str, kind: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{ticket_id}_{stamp}_{kind}"
    n = 2
    while path.exists():  # two runs in the same second must not share a folder
        path = RUNS_DIR / f"{ticket_id}_{stamp}-{n}_{kind}"
        n += 1
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_meta(path: Path, ticket_id: str, kind: str, usage: Dict[str, Any], extra: Optional[dict] = None) -> None:
    meta = {"ticket_id": ticket_id, "kind": kind, "model": MODEL, "usage": usage,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    meta.update(extra or {})
    (path / "meta.json").write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------------------
# Deep agent run
# ---------------------------------------------------------------------------

def run_review(ticket_id: str, requirement_doc: str, simulated_failures: int = 0,
               max_turns: int = dh.MAX_TURNS) -> Dict[str, Any]:
    check_api_key()
    set_ticket(ticket_id, requirement_doc)
    agent = dh.build_review_agent(model=MODEL, max_turns=max_turns,
                                  simulated_failures=simulated_failures)
    with get_usage_metadata_callback() as cb:
        run = dh.run_review_loop_live(agent, ticket_id=ticket_id)
    usage = _usage(cb)

    folder = _archive_dir(ticket_id, "review")
    (folder / "analysis.md").write_text(run.analysis_markdown)
    dh.save_deepagent_trace(run, str(folder / "trace.json"))
    _write_meta(folder, ticket_id, "review", usage, {"turns": run.turns, "counts": run.counts})
    return {"kind": "review", "ticket_id": ticket_id, "requirement_doc": requirement_doc,
            "run": run, "analysis_markdown": run.analysis_markdown,
            "usage": usage, "archive": str(folder)}


# ---------------------------------------------------------------------------
# Grade-only check (new)
# ---------------------------------------------------------------------------

_CHECK_SYSTEM = """You are a strict grader. Judge the ANALYSIS file against every criterion in the RUBRIC. \
Use the REQUIREMENT only to decide coverage.

For each criterion, set "passed" to true only if you can quote the text that satisfies it verbatim, \
with its line number. If you cannot quote it, it is not there, so set "passed" to false and write a short, \
actionable "gap". Do not pass a criterion because the file looks like the sort of file that would satisfy it. \
Apply each criterion literally: where a criterion names a specific labelled line, only that exact label \
satisfies it. Where it asks for coverage, check every item rather than sampling.

Return ONLY a JSON object, no other text, in this shape:
{"criteria": [{"name": "<criterion name>", "passed": true, "quote": "line N: <verbatim text>", "gap": ""}]}
Include one entry per criterion, in rubric order."""


def _criterion_names(rubric: str) -> List[str]:
    return [m.group(1) for m in re.finditer(r"^-\s*([A-Za-z_ ]+):", rubric, re.MULTILINE)]


def _parse_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"Grader did not return JSON. Got: {text[:300]!r}")
    return json.loads(text[start:end + 1])


def run_check(ticket_id: str, requirement_doc: str, analysis_markdown: str,
              rubric: str = RUBRIC) -> Dict[str, Any]:
    """Grade an existing analysis.md once. No revision, no agent."""
    check_api_key()
    if not analysis_markdown.strip():
        raise ValueError("There is no report to check yet. Run the deep agent first.")

    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(analysis_markdown.splitlines(), 1))
    user = (f"REQUIREMENT:\n{requirement_doc}\n\nRUBRIC:\n{rubric}\n\n"
            f"ANALYSIS (line-numbered):\n{numbered}")

    llm = init_chat_model(MODEL, temperature=0)
    with get_usage_metadata_callback() as cb:
        reply = llm.invoke([("system", _CHECK_SYSTEM), ("user", user)])
    usage = _usage(cb)

    content = reply.content
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    data = _parse_json(content)
    criteria = data.get("criteria", [])

    # Anything the grader skipped counts as failed, in rubric order.
    by_name = {str(c.get("name", "")).strip().lower(): c for c in criteria}
    rows = []
    for name in _criterion_names(rubric):
        c = by_name.get(name.lower(), {})
        passed = bool(c.get("passed"))
        rows.append({"name": name, "passed": passed, "quote": c.get("quote", ""),
                     "gap": "" if passed else (c.get("gap") or "Grader gave no evidence for this criterion.")})

    folder = _archive_dir(ticket_id, "check")
    (folder / "analysis.md").write_text(analysis_markdown)
    (folder / "check.json").write_text(json.dumps({"criteria": rows}, indent=2))
    _write_meta(folder, ticket_id, "check", usage)
    return {"kind": "check", "ticket_id": ticket_id, "criteria": rows,
            "all_passed": all(r["passed"] for r in rows), "usage": usage, "archive": str(folder)}


# ---------------------------------------------------------------------------
# Verification loop
# ---------------------------------------------------------------------------

def run_verification(ticket_id: str, requirement_doc: str, iterations: int = 3,
                     max_turns: int = 40, simulated_failures: int = 0) -> Dict[str, Any]:
    check_api_key()
    set_ticket(ticket_id, requirement_doc)
    agent = dh.build_review_agent(model=MODEL, grader_model=MODEL, rubric_iterations=iterations,
                                  max_turns=max_turns, simulated_failures=simulated_failures)
    with get_usage_metadata_callback() as cb:
        run = dh.run_verification_loop_live(agent, ticket_id=ticket_id, rubric=RUBRIC)
    usage = _usage(cb)

    folder = _archive_dir(ticket_id, "verification")
    (folder / "analysis.md").write_text(run.analysis_markdown)
    dh.save_verification_trace(run, str(folder / "trace.json"))
    _write_meta(folder, ticket_id, "verification", usage,
                {"status": run.status, "grading_passes": len(run.grading)})
    return {"kind": "verification", "ticket_id": ticket_id, "requirement_doc": requirement_doc,
            "run": run, "analysis_markdown": run.analysis_markdown,
            "usage": usage, "archive": str(folder)}


# ---------------------------------------------------------------------------
# History (newest 30 reviews)
# ---------------------------------------------------------------------------

import history  # noqa: E402

_STORE = None


def history_store():
    """Neon when NEON_DATABASE_URL is set and reachable, otherwise runs/history.json."""
    global _STORE
    if _STORE is None:
        _STORE = history.get_store(RUNS_DIR / "history.json")
    return _STORE


def history_description() -> str:
    store = history_store()
    return store.describe() + (f". {store.note}" if store.note else "")


def _hashes():
    return history.text_hash(read_agents_md()), history.text_hash(RUBRIC)


def save_review(res: Dict[str, Any], source: str = "manual", commit: str = "",
                actor: str = "", trigger: str = "") -> str:
    """Start a history record from a deep agent run. Returns the record id.

    source is "manual" for runs from the app and "event" for runs started by GitHub.
    """
    rec = history.base_record(res["ticket_id"], res["requirement_doc"], MODEL, *_hashes(),
                              source=source, commit=commit, actor=actor, trigger=trigger)
    history.attach_agent(rec, res)
    store = history_store()
    store.add(rec)
    store.prune(history.MAX_RUNS)
    return rec["id"]


def save_check(review_id: str, res: Dict[str, Any]) -> None:
    store = history_store()
    rec = store.get(review_id)
    if rec is None:
        raise KeyError("That review is no longer in the history (only the newest 30 are kept).")
    history.attach_check(rec, res)
    store.update(review_id, rec)


def save_verify(review_id: Optional[str], ticket_id: str, requirement_doc: str,
                res: Dict[str, Any]) -> str:
    """Attach a verification loop result to a review, or start a new record."""
    store = history_store()
    rec = store.get(review_id) if review_id else None
    is_new = rec is None
    if is_new:
        rec = history.base_record(ticket_id, requirement_doc, MODEL, *_hashes())
    history.attach_verify(rec, res)
    if is_new:
        store.add(rec)
        store.prune(history.MAX_RUNS)
    else:
        store.update(rec["id"], rec)
    return rec["id"]


def list_history() -> List[Dict[str, Any]]:
    return history_store().list(history.MAX_RUNS)


def already_reviewed(ticket_id: str, requirement_doc: str) -> bool:
    """True if the history already holds a deep agent run for this exact ticket text."""
    wanted = history.text_hash(requirement_doc)
    return any(r.get("ticket_id") == ticket_id and r.get("ticket_hash") == wanted
               and "agent" in r.get("kinds", [])
               for r in list_history())


def get_record(rec_id: str) -> Optional[Dict[str, Any]]:
    return history_store().get(rec_id)
