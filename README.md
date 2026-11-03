# Requirements Review Agent

## Objective
- Use loop engineering to build agents that do a task, verify their own work, and improve over time
- Phase 1 (built): an agent reviews Jira requirements, and a verification loop checks the result
- Phase 2 (next): event-based agents. Phase 3 (next): self-improving agents

## How it works

### At a glance
1. Pick a Jira (or add one), and edit its text if needed
2. **Run deep agent** produces a review report (`analysis.md`)
3. **Check against rubric** scores the report and lists gaps
4. **Run verification loop** lets the agent fix the gaps and be re-checked
5. Every review is saved in the **Run history (last 30)** tab
6. Event-driven: changing `jira_tickets.json` on GitHub starts a review automatically

### Use case: reviewing Jira requirements
- The agent reads a Jira requirement and reviews it using the instructions in `workspace/memories/AGENTS.md`
- It writes a report (`analysis.md`) with five sections:
  - Confirmed Facts: only what the ticket states
  - Ambiguities: the exact clause and why it is vague
  - Clarification Questions: what must be answered before work starts
  - Assumptions: what a developer would assume otherwise
  - Summary: two or three sentences
- Sample tickets are in `jira_tickets.json`. You can edit them, paste new text, or upload a .txt/.md file
- To fix a gap, edit the ticket text and run again, then compare in Run history

### Rubric: checking the agent
- The rubric is in `rubric.md` (edit it to change what "good" means)
- Three criteria:
  - Completeness: every clause of the requirement is addressed
  - Clarity: every ambiguity has a "Resolved by:" line naming what would settle it
  - Depth: every ambiguity has an "Edge case:" line naming a real production failure
- A grader checks the report against each criterion and must quote evidence

### Agent and verification loop in the app
- **Run deep agent:** the agent fetches the ticket, writes `analysis.md` section by section, and finishes. The app shows the report, step-by-step trace and token usage
- **Check against rubric:** the grader scores the report once and lists the gaps. The agent is not run again
- **Run verification loop:** the agent runs, the grader checks the file, failed criteria are sent back as feedback, the agent revises, and the grader checks again (up to 3 passes)
- Runs happen one at a time (a second user is asked to retry)

### Run history
- Every review is saved: one row per review, with badges for the steps done (Agent, Rubric check, Verified), the rubric result, tokens and model
- A rubric check or verification loop on the same ticket text is added to the same review. If the ticket text was edited, a verification loop starts a new row
- The report shown for a row is the verified one if it exists, otherwise the deep agent report
- Keeps the newest 30 reviews. Export as JSON or CSV from the tab
- A Source tag separates **Manual** runs (from the app) from **Event** runs (started by GitHub). Filter by Source in the tab. Event runs also show the commit and the person
- Rubric check and verification loop can be run on any row, including event runs
- Stored in a local file (`runs/history.json`) by default, or in Neon (hosted Postgres) when `NEON_DATABASE_URL` is set. Files for each run (report, trace, token usage) are also written to `runs/`

### Event-driven runs (GitHub)
- A GitHub Action (`.github/workflows/review-on-change.yml`) starts when `jira_tickets.json` changes on `main`, or when someone clicks **Run workflow** and enters a ticket ID
- Only people with write access to the repo can start it
- It reviews **one** ticket per run: the first new or changed ticket not already reviewed with the same text. Other changed tickets are listed as not reviewed and can be started with **Run workflow**
- It runs the deep agent only, saves the full report to Run history (Source = Event), and posts a short summary with a link as a comment on the commit and on the Action run page
- The rubric check and verification loop stay on demand from the app
- Runs one at a time. The run stops early if the history is not in Neon, so no tokens are spent on results nobody can see
- Setup in the GitHub repo (Settings > Secrets and variables > Actions):
  - Secrets: `GROQ_API_KEY`, `NEON_DATABASE_URL` (the same Neon database the hosted app uses)
  - Variable: `APP_URL` (the hosted app address, used for the link in the comment)
  - Actions must be enabled for the repo

### Tools and model
- Model: `gpt-oss-120b` on Groq (`groq:openai/gpt-oss-120b`)
- Agent framework: `deepagents`
- LangChain: tools, middleware (turn limit and grader) and the model connector (`langchain-groq`)
- LangGraph: the agent's execution graph, saved state during the verification loop, and the step streaming behind the trace
- UI: Streamlit
- Display: pandas, mistune. Config: python-dotenv
- Run history: Neon through `psycopg` (hosted app), local file otherwise
- Event trigger: GitHub Actions (`run_event.py`)
- Optional tracing: LangSmith (set keys in `.env`)

## Next phases
- **More event sources:** the GitHub trigger on `jira_tickets.json` is built. Next are GitHub Issues with comment commands, then Jira
- **Self-improving agents:** find patterns across many saved runs and propose improvements to the agent instructions
- **Login and personal keys:** let each user sign in and enter their own Groq key instead of the shared one

## Setup (local)
1. Unzip and open a terminal in the project folder (Python 3.10 or newer is required)
2. `python3 -m venv .venv`
3. `source .venv/bin/activate` (Windows: `.venv\Scripts\activate`)
4. `pip install -r requirements.txt`
5. `cp .env.example .env` (Windows: `copy .env.example .env`)
6. Open `.env` and set `GROQ_API_KEY=` to your key from console.groq.com/keys
7. Leave `NEON_DATABASE_URL` out of your local `.env`. Local runs are saved in `runs/history.json`, which keeps them apart from the hosted app's history

## Usage
1. `streamlit run app.py`
2. Open the local URL it prints (usually http://localhost:8501)
3. Pick a Jira or choose "New Jira...", edit the text, click **Run deep agent**
4. Read the analysis.md, Trace, Run history and AGENTS.md tabs
5. Click **Check against rubric**, then **Run verification loop**

## Hosting for free
- **Streamlit Community Cloud:**
  - Deploy the GitHub repo at share.streamlit.io with `app.py` as the main file
  - Add `GROQ_API_KEY` in the app's Secrets settings (see `.streamlit/secrets.toml.example`)
  - Add `NEON_DATABASE_URL` there too, so the hosted history survives restarts. Use Neon for the hosted app only
  - Everyone who opens the link shares one key and its limits

## Files
- `app.py` screen, `core.py` run logic, `history.py` run history storage, `run_event.py` event-driven runner for GitHub Actions
- `deepagent_helpers.py`, `visualization_helpers.py` agent and display helpers
- `rubric.md`, `workspace/memories/AGENTS.md`, `jira_tickets.json` rubric, instructions and tickets
- `.github/workflows/review-on-change.yml` the GitHub Action
