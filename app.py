"""
Streamlit UI for the Requirements Review agent.

Run:  streamlit run app.py      (then open the printed local URL)

Flow: pick or add a Jira -> edit its text -> Run deep agent -> read the report
and trace -> check against the rubric -> optionally run the verification loop.
Every review is saved to the run history (Neon if configured, else a local file).
All logic lives in core.py and history.py; this file only wires the screen.
"""

import html
import json
import os
import threading
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

st.set_page_config(page_title="Requirements review agent", layout="wide")

# Layout polish: sticky header, less top space, larger small text, tidier rows.
st.html("""
<style>
html { font-size: 17.5px; }
header[data-testid="stHeader"] { height: 2.5rem; }
div[data-testid="stMainBlockContainer"], .block-container { padding-top: 3rem; padding-bottom: 2rem; }
div:has(> .st-key-app_header) {
    position: sticky; top: 3.75rem; z-index: 990;
    background: var(--background-color, #ffffff);
}
.st-key-app_header { padding: 0.3rem 0 0.4rem 0; border-bottom: 1px solid rgba(128,128,128,0.25); }
.st-key-app_header h1 { font-size: 1.9rem; padding: 0; margin: 0; }
h1 { padding-top: 0; margin-top: 0; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { font-size: 1rem !important; }
[data-testid="stFileUploaderDropzoneInstructions"] * { font-size: 1rem !important; }
[data-testid="stWidgetLabel"] p { font-size: 1rem !important; }
[data-testid="stTab"] p { font-size: 1.15rem !important; }
div[data-testid="stButton"] button p, div[data-testid="stDownloadButton"] button p { font-size: 1rem !important; }
</style>
""")

load_dotenv(override=True)
# On Streamlit Community Cloud the keys come from the app's Secrets settings.
for _key in ("GROQ_API_KEY", "NEON_DATABASE_URL"):
    try:
        if not os.environ.get(_key) and _key in st.secrets:
            os.environ[_key] = st.secrets[_key]
    except Exception:  # noqa: BLE001  (no secrets file locally is fine)
        pass

try:
    import core  # noqa: E402  (after the keys are set)
except json.JSONDecodeError as _e:
    st.error(f"jira_tickets.json is not valid JSON: line {_e.lineno}, column {_e.colno}, {_e.msg}. "
             "Fix the file in the repository and reload the app.")
    st.stop()
import history  # noqa: E402
from visualization_helpers import (  # noqa: E402
    render_report, render_review_trace, render_verification_summary,
    render_grading_grid, render_grader_feedback, render_report_progression,
)

NEW = "New Jira..."
state = st.session_state


@st.cache_resource
def run_lock() -> threading.Lock:
    """One agent run at a time: runs share one working folder and ticket store."""
    return threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _choices():
    return core.list_tickets() + [NEW]


def _resolve_id() -> str:
    if state["ticket"] == NEW:
        return (state.get("new_id") or "").strip() or core.next_custom_id()
    return state["ticket"]


def _page(body: str, height: int = 520) -> None:
    """Show helper-generated HTML in a scrollable box.

    st.html sanitizes the markup and ignores scripts. That matters here because
    the report text comes from a model and from ticket text a user can paste.
    """
    with st.container(height=height, border=True):
        st.html(f"<div style='background:#fff;color:#1a1a1a;padding:6px;border-radius:8px'>{body}</div>")


def _check_html(result) -> str:
    rows = ""
    for c in result["criteria"]:
        colour = "#15803d" if c["passed"] else "#b91c1c"
        label = "Pass" if c["passed"] else "Fail"
        detail = html.escape(c["quote"] if c["passed"] else c["gap"])
        rows += (f"<tr><td style='padding:6px 10px'>{html.escape(c['name'].title())}</td>"
                 f"<td style='padding:6px 10px;color:{colour};font-weight:700'>{label}</td>"
                 f"<td style='padding:6px 10px'>{detail}</td></tr>")
    verdict = "All criteria passed." if result["all_passed"] else "Some criteria failed. See the gaps below."
    return (f"<p style='margin:4px 0 8px 0;font-family:sans-serif'><b>{verdict}</b></p>"
            "<table style='width:100%;border-collapse:collapse;font-size:0.95em;font-family:sans-serif'>"
            "<tr style='background:#1f2937;color:#fff;text-align:left'>"
            "<th style='padding:6px 10px'>Criterion</th><th style='padding:6px 10px'>Result</th>"
            f"<th style='padding:6px 10px'>Evidence or gap</th></tr>{rows}</table>")


def _rel(path: str) -> str:
    try:
        return str(Path(path).relative_to(core.BASE_DIR))
    except ValueError:
        return path


def _guarded(label, fn):
    """Run fn() under the single-run lock, with a spinner and error display."""
    lock = run_lock()
    if not lock.acquire(blocking=False):
        st.warning("Another run is in progress. Try again in a minute.")
        return None
    try:
        with st.spinner(label):
            return fn()
    except Exception as e:  # noqa: BLE001
        st.error(f"Failed: {e}")
        return None
    finally:
        lock.release()


def _save_history(label, fn):
    """History problems must never lose the run the user just made."""
    try:
        out = fn()
        state["history"] = None  # reload the list next time it is shown
        return out
    except Exception as e:  # noqa: BLE001
        st.warning(f"The run worked, but it could not be saved to the history ({label}): {e}")
        return None


def _history() -> list:
    if state.get("history") is None:
        try:
            state["history"] = core.list_history()
            state["history_error"] = ""
        except Exception as e:  # noqa: BLE001
            state["history"] = []
            state["history_error"] = f"Could not load the history: {e}"
    return state["history"]


# ---------------------------------------------------------------------------
# Session setup (before any widget is created)
# ---------------------------------------------------------------------------

if "ticket" not in state:
    first = core.list_tickets()[0]
    state["ticket"] = first
    state["text"] = core.get_ticket_text(first)
if "_set_ticket" in state:  # set by a handler that wants to switch the selection
    tid = state.pop("_set_ticket")
    state["ticket"] = tid
    state["text"] = core.get_ticket_text(tid)


def _on_ticket_change():
    state["text"] = "" if state["ticket"] == NEW else core.get_ticket_text(state["ticket"])


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

with st.container(key="app_header"):
    st.title("Requirements review agent")
    st.caption(f"Model: `{core.MODEL}`")

st.subheader("1. Choose and edit the Jira")
left, right = st.columns([1, 3])
with left:
    st.selectbox("Jira", _choices(), key="ticket", on_change=_on_ticket_change)
    if state["ticket"] == NEW:
        st.text_input("New Jira ID (blank for automatic)", key="new_id")
    # Upload is switched off for now. To bring it back, remove the leading "# " on the lines below.
    # up = st.file_uploader("Upload Jira .txt or .md", type=["txt", "md"], key="upload")
    # if up is not None and state.get("_upload_id") != up.file_id:
    #     state["_upload_id"] = up.file_id
    #     state["text"] = up.getvalue().decode("utf-8", "replace").strip()
with right:
    st.text_area("Requirement text", key="text", height=190)
    save_clicked = st.button("Save Jira Updates", key="save_btn")

r1, _pad = st.columns([1.3, 7])
run_clicked = r1.button("Run deep agent", key="run_btn", type="primary", width="stretch")

if save_clicked:
    if not state["text"].strip():
        st.error("Enter requirement text first.")
    else:
        tid = _resolve_id()
        core.set_ticket(tid, state["text"])
        path = core.save_tickets_to_json()
        state["_set_ticket"] = tid
        state["notice"] = f"Saved {tid} to `{path.name}`."
        st.rerun()

if run_clicked:
    if not state["text"].strip():
        st.error("Enter requirement text first.")
    else:
        tid = _resolve_id()
        res = _guarded("Running the deep agent. This can take a minute or two...",
                       lambda: core.run_review(tid, state["text"]))
        if res:
            run = res["run"]
            state["prev_review"] = state.get("last_review")
            state["last_review"] = res
            state["last_review_id"] = _save_history("deep agent run", lambda: core.save_review(res))
            state["review_view"] = {
                "ticket_id": tid,
                "report": render_report(run.analysis_markdown or "(the agent produced no report)",
                                        title=f"analysis.md ({tid})", icon="").data,
                "trace": render_review_trace(run.trace).to_html(),
                "info": (f"{run.turns} model turns, {len(run.trace)} steps. Items per section: "
                         f"{run.counts}, summary {run.summary_words} words.\n\n"
                         f"{core.usage_line(res['usage'])}\n\nSaved to `{_rel(res['archive'])}`"),
                "markdown": run.analysis_markdown,
            }
            state["check_view"] = None
            if state["ticket"] == NEW:
                state["_set_ticket"] = tid
                state["notice"] = "Deep agent run finished."
                st.rerun()
            st.success("Deep agent run finished.")

if state.get("notice"):
    st.success(state.pop("notice"))

st.subheader("2. Deep agent result")
view = state.get("review_view")
HIST_TAB = "Run history (last 30)"
linked_run = st.query_params.get("run", "")
tab_report, tab_trace, tab_hist, tab_agents = st.tabs(
    ["Analysis md", "Trace", HIST_TAB, "Agents md (read-only)"],
    default=HIST_TAB if linked_run else None)
with tab_report:
    if view:
        st.markdown(view["info"])
        _page(view["report"], 620)
        st.download_button("Download analysis.md", view["markdown"], file_name="analysis.md")
    else:
        st.info("Run the deep agent to see the report here.")
with tab_trace:
    if view:
        _page(view["trace"], 460)
    else:
        st.info("Run the deep agent to see the step-by-step trace here.")
with tab_hist:
    records = _history()
    top1, top2, top3 = st.columns([3.4, 1, 1.6], vertical_alignment="center")
    top1.caption(f"Stored in: {core.history_description()}")
    if top2.button("Refresh", key="hist_refresh", width="stretch"):
        state["history"] = None
        st.rerun()
    if state.get("history_error"):
        st.error(state["history_error"])
    if not records:
        st.info("No runs saved yet. Run the deep agent and it will appear here.")
    else:
        top3.download_button("Export summary CSV",
                             pd.DataFrame(history.summary_rows(records)).to_csv(index=False),
                             file_name="run_history.csv", mime="text/csv",
                             key="hist_export_csv", width="stretch")
        source_filter = st.segmented_control("Source", ["All", "Manual", "Event-driven"],
                                             default="All", key="hist_source", selection_mode="single") or "All"
        wanted = {"All": None, "Manual": "manual", "Event-driven": "event"}[source_filter]
        shown_records = [r for r in records if wanted is None or history.source_of(r) == wanted]
        if not shown_records:
            st.info(f"No {source_filter.lower()} runs in the last 30.")
        else:
            summary = pd.DataFrame(history.summary_rows(shown_records))
            event = st.dataframe(summary, hide_index=True, width="stretch", on_select="rerun",
                                 selection_mode="single-row", key="hist_table")
            picked = event.selection.rows if event and event.selection else []
            if picked and picked[0] < len(shown_records):
                rec, how = shown_records[picked[0]], "Showing the selected run."
                idx = picked[0]
            else:
                linked = next((r for r in records if r["id"] == linked_run), None) if linked_run else None
                if linked_run and linked is None:
                    linked = core.get_record(linked_run)  # may be older than the newest 30
                if linked:
                    rec, how = linked, "Opened from a link."
                    idx = next((i for i, r in enumerate(shown_records) if r["id"] == linked["id"]), None)
                else:
                    rec, how, idx = shown_records[0], "Showing the newest run. Click a row above to open another.", 0
                    if linked_run:
                        how = "That linked run is no longer in the history. " + how
            when = rec["created_at"][:16].replace("T", " ") + " UTC"
            st.markdown(f"**{rec.get('ticket_id', '')}**, saved {when}, "
                        f"{int(rec.get('tokens_total', 0) or 0):,} tokens. {how}")
            badge_colours = {"agent": "blue", "check": "orange", "verify": "green"}
            bcols = st.columns([1, 1, 1, 1, 4])
            with bcols[0]:
                st.badge(history.source_label(rec), color="violet" if history.source_of(rec) == "event" else "gray")
            for i, kind in enumerate(rec.get("kinds", [])):
                with bcols[i + 1]:
                    st.badge(history.STEP_LABELS.get(kind, kind), color=badge_colours.get(kind, "gray"))
            if history.source_of(rec) == "event":
                who = f" by {rec['actor']}" if rec.get("actor") else ""
                st.caption(f"Run by GitHub on commit {(rec.get('commit') or '')[:7] or 'unknown'}{who}"
                           + (" (started by hand)." if rec.get("trigger") == "dispatch" else "."))
            st.caption(f"Rubric result: {history.rubric_result(rec)}"
                       + (". Report shown is the verified version." if rec.get("verify") else "."))

            latest = (state.get("last_review") or {}).get("analysis_markdown")
            compare = st.checkbox("Compare with the latest run in this session", key="hist_compare",
                                  disabled=not latest)
            shown = history.result_report(rec) or "(no report saved)"
            if compare and latest:
                c_a, c_b = st.columns(2)
                with c_a:
                    st.caption("Selected run")
                    _page(render_report(shown, title="Selected run", icon="", muted=True).data, 560)
                with c_b:
                    st.caption("Latest run")
                    _page(render_report(latest, title="Latest run", icon="").data, 560)
            else:
                _page(render_report(shown, title="Result report", icon="").data, 560)

            agent_report = (rec.get("agent") or {}).get("report", "")
            if rec.get("verify") and agent_report:
                with st.expander("Deep agent report (before verification)"):
                    _page(render_report(agent_report, title="Deep agent report", icon="",
                                        muted=True).data, 480)
            if rec.get("check"):
                with st.expander("Rubric check"):
                    _page(_check_html(rec["check"]), 230)
            if rec.get("verify"):
                with st.expander("Verification loop"):
                    v = rec["verify"]
                    seq = " then ".join(g["result"] for g in v.get("grading", [])) or "grader never ran"
                    st.markdown(f"Verdicts: {seq}. Final status: {v.get('status') or 'unknown'}.")

            st.markdown("**Run the next steps on this run**")
            n1, n2, _npad = st.columns([1.5, 1.7, 4])
            check_here = n1.button("Rubric check", key="hist_check_btn", width="stretch",
                                   disabled=not agent_report)
            verify_here = n2.button("Verification loop", key="hist_verify_btn", width="stretch")
            if check_here:
                res = _guarded("Checking this run's report against the rubric...",
                               lambda: core.run_check(rec["ticket_id"], rec.get("ticket_text", ""), agent_report))
                if res:
                    _save_history("rubric check", lambda: core.save_check(rec["id"], res))
                    if state.get("history") is None:
                        st.rerun()
            if verify_here:
                def _verify_here():
                    keep = core.get_ticket_text(rec["ticket_id"])
                    try:
                        return core.run_verification(rec["ticket_id"], rec.get("ticket_text", ""))
                    finally:
                        if keep:  # do not leave this run's older text in the ticket store
                            core.set_ticket(rec["ticket_id"], keep)
                res = _guarded("Running the verification loop on this ticket. This can take several minutes...",
                               _verify_here)
                if res:
                    _save_history("verification loop", lambda: core.save_verify(
                        rec["id"], rec["ticket_id"], rec.get("ticket_text", ""), res))
                    if state.get("history") is None:
                        st.rerun()
with tab_agents:
    st.code(core.read_agents_md(), language="markdown")

st.subheader("3. Evaluate agent performance")
with st.expander("rubric.md (read-only)", expanded=True):
    st.code(core.RUBRIC, language="markdown", wrap_lines=True)
e1, e2, _epad = st.columns([1.5, 1.5, 5])
check_clicked = e1.button("Check against rubric", key="check_btn", width="stretch")
loop_clicked = e2.button("Run verification loop", key="loop_btn", width="stretch")

if check_clicked:
    last = state.get("last_review")
    if not last:
        st.warning("Run the deep agent first.")
    else:
        res = _guarded("Checking the report against the rubric...",
                       lambda: core.run_check(last["ticket_id"], last["requirement_doc"],
                                              last["analysis_markdown"]))
        if res:
            state["check_view"] = {"html": _check_html(res), "usage": core.usage_line(res["usage"])}
            if state.get("last_review_id"):
                _save_history("rubric check", lambda: core.save_check(state["last_review_id"], res))
                if state.get("history") is None:  # saved: refresh the history tab above
                    st.rerun()

if loop_clicked:
    if not state["text"].strip():
        st.error("Enter requirement text first.")
    else:
        tid = _resolve_id()
        res = _guarded("Running the verification loop. This can take several minutes...",
                       lambda: core.run_verification(tid, state["text"]))
        if res:
            run = res["run"]
            g = bool(run.grading)
            state["loop_view"] = {
                "summary": render_verification_summary(run).data if g else "The grader never ran.",
                "grid": render_grading_grid(run).to_html() if g else "",
                "feedback": render_grader_feedback(run).data if g else "",
                "progress": (render_report_progression(res["requirement_doc"],
                                                       run.grading[0].analysis_markdown,
                                                       run.analysis_markdown).data if g else ""),
                "usage": f"{core.usage_line(res['usage'])}\n\nSaved to `{_rel(res['archive'])}`",
            }
            # Attach to the last review only if it was made from the same ticket text.
            last = state.get("last_review")
            same = bool(last and last["ticket_id"] == tid
                        and history.text_hash(last["requirement_doc"]) == history.text_hash(state["text"]))
            _save_history("verification loop", lambda: core.save_verify(
                state.get("last_review_id") if same else None, tid, state["text"], res))
            if state.get("history") is None:  # saved: refresh the history tab above
                st.rerun()

cv = state.get("check_view")
if cv:
    st.markdown("**Rubric check**")
    _page(cv["html"], 230)
    st.caption(cv["usage"])

lv = state.get("loop_view")
if lv:
    st.markdown("**Verification loop**")
    st.markdown(lv["summary"])
    if lv["grid"]:
        _page(lv["grid"], 260)
    if lv["feedback"]:
        with st.expander("What the grader told the agent"):
            _page(lv["feedback"], 520)
    if lv["progress"]:
        with st.expander("First draft vs final report"):
            _page(lv["progress"], 700)
    st.caption(lv["usage"])
