"""
Event-driven review, run by a GitHub Action.

Starts when jira_tickets.json changes on main, or when someone with write access
starts the workflow by hand. It reviews ONE ticket per run with the deep agent,
saves the result to the run history in Neon (Source = Event), and posts a short
summary with a link as a comment on the commit. The full report lives in the
app's Run history tab. The rubric check and verification loop stay on demand.

Environment (set by the workflow):
  GROQ_API_KEY, NEON_DATABASE_URL        repository secrets
  GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_ACTOR, GITHUB_SHA, GITHUB_RUN_ID,
  GITHUB_SERVER_URL, GITHUB_STEP_SUMMARY provided by GitHub
  BEFORE_SHA, AFTER_SHA                  commit range of the push
  TICKET_ID                              ticket to review (manual runs only)
  APP_URL                                address of the hosted app (repository variable)

Local test:  python run_event.py --before <sha> --after <sha> --allow-local
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

TICKETS_FILE = "jira_tickets.json"
ZERO_SHA = "0" * 40
BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Finding the ticket to review
# ---------------------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True, text=True)


def read_tickets_at(rev: str) -> Tuple[str, object]:
    """The tickets file at a commit, as (state, value).

    state is "ok" (value = the tickets), "no_commit", "no_file", or "invalid"
    (value = the error). A broken file is never treated as an empty one.
    """
    if not rev or rev == ZERO_SHA or _git("cat-file", "-e", f"{rev}^{{commit}}").returncode != 0:
        return "no_commit", None
    shown = _git("show", f"{rev}:{TICKETS_FILE}")
    if shown.returncode != 0:
        return "no_file", None
    try:
        data = json.loads(shown.stdout)
    except json.JSONDecodeError as e:
        return "invalid", e
    if not isinstance(data, dict):
        return "invalid", ValueError("the file must be a JSON object of ticket IDs and texts")
    return "ok", data


def base_tickets(before: str, after: str) -> Tuple[Optional[Dict[str, str]], str]:
    """The tickets to compare against, and a note on where they came from.

    Normally the version before the push. If that version is broken or missing, use
    the last readable earlier version instead. Returns (None, note) when earlier
    versions exist but none can be read, because then nobody can tell what changed.
    """
    state, value = read_tickets_at(before)
    if state == "ok":
        return value, ""
    if state == "no_file":
        return {}, "the file did not exist before this push"
    if before == ZERO_SHA or not before:
        return {}, "new branch"
    log = _git("log", "--format=%H", "-n", "50", after, "--", TICKETS_FILE)
    earlier = [sha for sha in log.stdout.split() if sha != after] if log.returncode == 0 else []
    for sha in earlier:
        state, value = read_tickets_at(sha)
        if state == "ok":
            return value, f"the previous version could not be read, so compared with the last valid one ({sha[:7]})"
        if state == "no_file":
            return {}, "the file did not exist earlier"
    if not earlier:
        return {}, "the file first appears in this push"
    return None, "no earlier version of the file could be read"


def tickets_now() -> Dict[str, str]:
    """The current tickets file. Raises json.JSONDecodeError if it is broken."""
    data = json.loads((BASE_DIR / TICKETS_FILE).read_text())
    if not isinstance(data, dict):
        raise json.JSONDecodeError("the file must be a JSON object of ticket IDs and texts", "", 0)
    return data


def changed_tickets(old: Dict[str, str], new: Dict[str, str]) -> List[str]:
    """New tickets first, then edited ones, each group in file order."""
    added = [tid for tid in new if tid not in old]
    edited = [tid for tid, text in new.items() if tid in old and old[tid] != text]
    return added + edited


def choose(changed: List[str], new: Dict[str, str],
           reviewed: Callable[[str, str], bool]) -> Tuple[Optional[str], List[str], List[str]]:
    """Pick the first changed ticket that has not been reviewed with this exact text.

    Returns (chosen, skipped, already_reviewed). Only one ticket is reviewed per run.
    """
    already = [t for t in changed if reviewed(t, new[t])]
    todo = [t for t in changed if t not in already]
    return (todo[0] if todo else None), todo[1:], already


# ---------------------------------------------------------------------------
# The comment
# ---------------------------------------------------------------------------

def _code(text: str, limit: int = 140) -> str:
    """Show model or ticket text as code, so it cannot ping people or inject markup."""
    clean = " ".join(str(text).replace("`", "'").replace("**", "").split())
    if len(clean) > limit:
        clean = clean[:limit].rstrip() + "..."
    return f"`{clean}`"


def app_link(app_url: str, record_id: str) -> str:
    return f"{app_url.rstrip('/')}/?run={record_id}" if app_url else ""


def summary_markdown(res: dict, record_id: str, *, app_url: str = "", commit: str = "",
                     skipped: Optional[List[str]] = None, already: Optional[List[str]] = None,
                     run_url: str = "") -> str:
    import core
    run = res["run"]
    sections = core.dh.split_sections(run.analysis_markdown)
    top = [b for b in core.dh.bullets(sections.get("Ambiguities", ""))][:3]
    counts = run.counts
    usage = res["usage"]
    lines = [
        f"### Requirement review: {_code(res['ticket_id'])}",
        f"- Facts: {counts.get('Confirmed Facts', 0)}, ambiguities: {counts.get('Ambiguities', 0)}, "
        f"questions: {counts.get('Clarification Questions', 0)}, assumptions: {counts.get('Assumptions', 0)}",
    ]
    if top:
        lines.append("- Top ambiguities:")
        lines += [f"  - {_code(t)}" for t in top]
    tokens = usage.get("total_tokens") or 0
    lines.append(f"- Deep agent only, {run.turns} model turns, "
                 + (f"{tokens:,} tokens" if tokens else "tokens not reported")
                 + f", model {_code(core.MODEL)}")
    link = app_link(app_url, record_id)
    lines.append("")
    lines.append(f"**[Open the full report in Run history]({link})**" if link
                 else f"Full report: Run history tab in the app, run id {_code(record_id)}")
    lines.append("")
    lines.append("The rubric check and verification loop run on demand from the app.")
    if skipped:
        lines.append(f"Not reviewed in this run (one ticket per run): {', '.join(_code(t) for t in skipped)}. "
                     "Start them with the Run workflow button.")
    if already:
        lines.append(f"Already reviewed with this text: {', '.join(_code(t) for t in already)}.")
    if run_url:
        lines.append(f"[Action run]({run_url})")
    return "\n".join(lines)


def failure_markdown(ticket_id: str, error: Exception, run_url: str = "") -> str:
    msg = _code(f"{type(error).__name__}: {error}", 300)
    out = f"### Requirement review failed: {_code(ticket_id)}\n{msg}\n"
    return out + (f"\n[Action run]({run_url})" if run_url else "")


def post_commit_comment(repo: str, sha: str, body: str, token: str) -> bool:
    """Comment on a commit. Returns False (and prints why) if GitHub refuses."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/commits/{sha}/comments",
        data=json.dumps({"body": body}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                 "User-Agent": "requirements-review-agent"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"Could not post the commit comment: HTTP {e.code} {e.reason}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"Could not post the commit comment: {type(e).__name__}: {e}", file=sys.stderr)
    return False


def problem_markdown(title: str, detail: str, run_url: str = "") -> str:
    out = f"### Requirement review stopped: {title}\n{detail}\n"
    return out + (f"\n[Action run]({run_url})" if run_url else "")


def write_job_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _is_rate_limit(error: Exception) -> bool:
    text = str(error).lower()
    return "rate limit" in text or "429" in text or "rate_limit" in text


def run_with_retry(fn, attempts: int = 3, wait: float = 65.0):
    """Groq's free plan limits tokens per minute, so wait and try again on rate limits."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1 or not _is_rate_limit(e):
                raise
            print(f"Rate limited, waiting {wait:.0f}s before attempt {i + 2} of {attempts}...")
            time.sleep(wait)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default=os.environ.get("BEFORE_SHA", ""))
    ap.add_argument("--after", default=os.environ.get("AFTER_SHA") or os.environ.get("GITHUB_SHA", ""))
    ap.add_argument("--ticket", default=os.environ.get("TICKET_ID", ""))
    ap.add_argument("--allow-local", action="store_true",
                    help="testing only: allow the history to be a local file instead of Neon")
    args = ap.parse_args(argv)

    import core  # after the environment is set

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    actor = os.environ.get("GITHUB_ACTOR", "")
    app_url = os.environ.get("APP_URL", "")
    sha = args.after
    run_url = ""
    if repo and os.environ.get("GITHUB_RUN_ID"):
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
                   f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    wait = float(os.environ.get("RETRY_WAIT_SECONDS", "65"))

    # Event runs must be visible in the app, so the history has to be in Neon.
    store = core.history_store()
    if store.backend != "neon" and not args.allow_local:
        print("Stopping before any tokens are used: the run history is not in Neon. "
              + (store.note or "Set the NEON_DATABASE_URL repository secret."), file=sys.stderr)
        return 1

    def stop(title: str, detail: str) -> int:
        body = problem_markdown(title, detail, run_url)
        print(body, file=sys.stderr)
        write_job_summary(body)
        if token and repo and sha:
            post_commit_comment(repo, sha, body, token)
        return 1

    try:
        new = tickets_now()
    except json.JSONDecodeError as e:
        return stop(f"{TICKETS_FILE} is not valid JSON",
                    f"Line {e.lineno}, column {e.colno}: {e.msg}. Fix the file and commit again. "
                    "No tokens were used.")

    if args.ticket:  # started by hand: review exactly this ticket
        if args.ticket not in new:
            print(f"Ticket {args.ticket!r} is not in {TICKETS_FILE}.", file=sys.stderr)
            return 1
        chosen, skipped, already, trigger = args.ticket, [], [], "dispatch"
    else:  # a push: review the first new or changed ticket
        base, note = base_tickets(args.before, args.after)
        if base is None:
            return stop("cannot tell which ticket changed",
                        f"Earlier versions of {TICKETS_FILE} are not valid JSON ({note}). "
                        "Start the review by hand: Run workflow and enter the ticket ID. "
                        "No tokens were used.")
        if note:
            print(f"Comparing tickets: {note}")
        changed = changed_tickets(base, new)
        chosen, skipped, already = choose(changed, new, core.already_reviewed)
        trigger = "push"

    if not chosen:
        note = ("No new or changed tickets to review."
                if not already else f"Already reviewed with this text: {', '.join(already)}.")
        print(note)
        write_job_summary(f"### Requirement review\n{note}")
        return 0

    print(f"Reviewing {chosen} (trigger: {trigger}, commit: {sha[:7]}, by: {actor or 'unknown'})")
    try:
        res = run_with_retry(lambda: core.run_review(chosen, new[chosen]), wait=wait)
        record_id = core.save_review(res, source="event", commit=sha, actor=actor, trigger=trigger)
        body = summary_markdown(res, record_id, app_url=app_url, commit=sha, skipped=skipped,
                                already=already, run_url=run_url)
        error = None
    except Exception as e:  # noqa: BLE001
        body, error = failure_markdown(chosen, e, run_url), e

    print(body)
    write_job_summary(body)
    if token and repo and sha:
        post_commit_comment(repo, sha, body, token)
    else:
        print("(No GitHub token, repository or commit: the comment was not posted.)")
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
