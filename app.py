"""
Streamlit UI for the Requirements Review agent.

Run:  streamlit run app.py      (then open the printed local URL)

Flow: pick or add a Jira -> edit its text -> Run deep agent -> read the report
and trace -> check against the rubric -> optionally run the verification loop.
All logic lives in core.py; this file only wires the screen.
"""

import html
import os
import threading
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

st.set_page_config(page_title="Requirements review agent", layout="wide")

# Layout polish: less space at the top, larger small text, tidier button rows.
st.html("""
<style>
html { font-size: 17.5px; }
header[data-testid="stHeader"] { height: 2.5rem; }
div[data-testid="stMainBlockContainer"], .block-container { padding-top: 3rem; padding-bottom: 2rem; }
h1 { padding-top: 0; margin-top: 0; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { font-size: 1rem !important; }
[data-testid="stFileUploaderDropzoneInstructions"] * { font-size: 1rem !important; }
[data-testid="stWidgetLabel"] p { font-size: 1rem !important; }
[data-testid="stTab"] p { font-size: 1.15rem !important; }
div[data-testid="stButton"] button p, div[data-testid="stDownloadButton"] button p { font-size: 1rem !important; }
</style>
""")

load_dotenv(override=True)
# On Streamlit Community Cloud the key comes from the app's Secrets settings.
try:
    if not os.environ.get("GROQ_API_KEY") and "GROQ_API_KEY" in st.secrets:
        os.environ["GROQ_API_KEY"] = st.secrets["GROQ_API_KEY"]
except Exception:  # noqa: BLE001  (no secrets file locally is fine)
    pass

import core  # noqa: E402  (after the key is set)
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

st.title("Requirements review agent")
st.caption(f"Model: `{core.MODEL}`")

st.subheader("1. Choose and edit the Jira")
left, right = st.columns([1, 3])
with left:
    st.selectbox("Jira", _choices(), key="ticket", on_change=_on_ticket_change)
    if state["ticket"] == NEW:
        st.text_input("New Jira ID (blank for automatic)", key="new_id")
    up = st.file_uploader("Upload Jira .txt or .md", type=["txt", "md"], key="upload")
    if up is not None and state.get("_upload_id") != up.file_id:
        state["_upload_id"] = up.file_id
        state["text"] = up.getvalue().decode("utf-8", "replace").strip()
with right:
    st.text_area("Requirement text", key="text", height=190)
    save_clicked = st.button("Save Jira Updates", key="save_btn")

r1, r2, _pad = st.columns([1.3, 3, 4], vertical_alignment="center")
run_clicked = r1.button("Run deep agent", key="run_btn", type="primary", width="stretch")
simulate = r2.checkbox("Simulate 2 Jira failures (retry demo)", key="simulate")

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
                       lambda: core.run_review(tid, state["text"],
                                               simulated_failures=2 if simulate else 0))
        if res:
            run = res["run"]
            state["prev_review"] = state.get("last_review")
            state["last_review"] = res
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
tab_report, tab_trace, tab_prev, tab_agents = st.tabs(
    ["Report", "Trace", "Previous run", "AGENTS.md (read-only)"])
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
with tab_prev:
    prev, last = state.get("prev_review"), state.get("last_review")
    if prev and last:
        pc, lc = st.columns(2)
        with pc:
            st.caption(f"Previous run ({prev['ticket_id']})")
            _page(render_report(prev["analysis_markdown"], title="Previous run", icon="", muted=True).data, 620)
        with lc:
            st.caption(f"Latest run ({last['ticket_id']})")
            _page(render_report(last["analysis_markdown"], title="Latest run", icon="").data, 620)
    else:
        st.info("No previous run yet. Edit the ticket and run again to compare.")
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
