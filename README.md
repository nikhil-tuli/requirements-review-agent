# Requirements Review Agent

App: https://requirements-review-agent.streamlit.app

## Objective
- Use loop engineering to build agents that do a task, verify their own work, and improve over time
- Phase 1 (built): an agent reviews Jira requirements, and a verification loop checks the result
- Phase 2 (built): event-based agents. A change to `jira_tickets.json` on GitHub starts a review automatically
- Phase 3 (next): self-improving agents

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
- Sample tickets are in `jira_tickets.json`. You can edit them or paste new text
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
- Keeps the newest 30 reviews. Export a summary as CSV from the tab
- A Source tag separates **Manual** runs (from the app) from **Event** runs (started by GitHub). Filter by Source in the tab. Event runs also show the commit and the person
- Rubric check and verification loop can be run on any row, including event runs
- Stored in a local file (`runs/history.json`) by default, or in Neon (hosted Postgres) when `NEON_DATABASE_URL` is set. Files for each run (report, trace, token usage) are also written to `runs/`

### Event-driven runs (GitHub)
- A GitHub Action (`.github/workflows/review-on-change.yml`) starts when `jira_tickets.json` changes on `main`
- Manual start: for a ticket already in the file (for example one that was skipped, or after a failed run), click **Run workflow** and enter its ID
- Only people with write access to the repo can start it
- It reviews **one** ticket per run: new tickets before edited ones, skipping any already reviewed with the same text. Other changed tickets are listed as not reviewed and can be started with **Run workflow**
- If the previous version of `jira_tickets.json` is broken, it compares with the last valid version. If there is no valid earlier version, or the current file is not valid JSON, it stops with a clear comment (the line and column for broken JSON) and uses no tokens
- It runs the deep agent only, saves the full report to Run history (Source = Event), and posts a short summary with a link as a comment on the commit and on the Action run page
- The rubric check and verification loop stay on demand from the app
- Runs one at a time
- Checks the Neon connection first. If it is not set up, the run stops before using any model tokens
- Setup in the GitHub repo (Settings > Secrets and variables > Actions):
  - Secrets: `GROQ_API_KEY`, `NEON_DATABASE_URL` (the same Neon database the hosted app uses)
  - Variable: `APP_URL` (the web address of the hosted app, such as `https://requirements-review-agent.streamlit.app`. The comment's link opens the app on that run)
  - Actions must be enabled for the repo

### Tools and model
- Model: `gpt-oss-120b` on Groq (`groq:openai/gpt-oss-120b`)
- UI: Streamlit. Display: pandas, mistune. Config: python-dotenv
- Run history: Neon through `psycopg` (hosted app), local file otherwise
- Event trigger: GitHub Actions (`run_event.py`)
- Optional tracing: LangSmith (set keys in `.env`)

### LangChain components and tools used
- `deepagents`:
  - `create_deep_agent`: builds the agent
  - `FilesystemBackend` and `FilesystemMiddleware`: file tools on the `workspace/` folder
  - `FilesystemPermission`: stops the agent from editing `AGENTS.md`
  - `RubricMiddleware`: the grader that sends failed criteria back to the agent
- `langchain`:
  - `init_chat_model`: loads the Groq model
  - `@tool`: defines the custom `fetch_requirement` tool
  - `ModelCallLimitMiddleware`: caps the number of model turns
- `langchain-groq`: the Groq chat model connector
- `langchain-core`: `get_usage_metadata_callback` counts tokens per run
- `langgraph`:
  - `InMemorySaver`: keeps state during the verification loop
  - The agent's graph (`.stream`, `.get_state`) gives the step trace and grader results
- Tools the agent can call:
  - Custom: `fetch_requirement` (reads the Jira ticket)
  - Built in: `write_file`, `edit_file`, `read_file` (also used by the grader), `ls`, `glob`, `grep`, `delete`, `task`

## Challenges
- **Groq's free plan token limit**
  - The free plan allows 8,000 tokens per minute for this model, and one agent turn sends roughly 5,000 tokens or more, growing as the run goes on
  - Handled: event runs wait and retry when a rate limit is hit. For regular use, add a payment method (Groq's Developer plan), which raises the limit
- **Run history lost on restart**
  - The hosted app's disk is temporary, so a local history file disappears whenever the app restarts
  - Handled: the hosted app and the GitHub Action keep history in Neon. Local runs use a local file. If Neon cannot be reached, the app falls back to the local file and says so

## Next phases
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
4. Read the Analysis md, Trace, Run history and Agents md tabs
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
