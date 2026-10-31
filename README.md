# Requirements Review Agent

## Objective
- Use loop engineering to build agents that do a task, verify their own work, and improve over time
- Phase 1 (built): an agent reviews Jira requirements, and a verification loop checks the result
- Phase 2 (next): event-based agents. Phase 3 (next): self-improving agents

## Current use case: reviewing Jira requirements
- The agent reads a Jira requirement and reviews it using the instructions in `workspace/memories/AGENTS.md`
- It writes a report (`analysis.md`) with five sections:
  - Confirmed Facts: only what the ticket states
  - Ambiguities: the exact clause and why it is vague
  - Clarification Questions: what must be answered before work starts
  - Assumptions: what a developer would assume otherwise
  - Summary: two or three sentences
- Sample tickets are in `jira_tickets.json`. You can edit them, paste new text, or upload a .txt/.md file

## Checking the agent with a rubric
- The rubric is in `rubric.md` (edit it to change what "good" means)
- Three criteria:
  - Completeness: every clause of the requirement is addressed
  - Clarity: every ambiguity has a "Resolved by:" line naming what would settle it
  - Depth: every ambiguity has an "Edge case:" line naming a real production failure
- A grader checks the report against each criterion and must quote evidence

## How the app works
- **Run deep agent:** the agent fetches the ticket, writes `analysis.md` section by section, and finishes. The app shows the report, step-by-step trace and token usage
- **Check against rubric:** the grader scores the report once and lists the gaps
- **Run verification loop:** the agent runs, the grader checks the file, failed criteria are sent back as feedback, the agent revises, and the grader checks again (up to 3 passes)
- **Fix and re-run:** edit the ticket, run again, and compare with the previous run
- Every run is saved in `runs/` (report, trace, token usage)
- Runs happen one at a time (a second user is asked to retry)

## Tools and model
- Model: `gpt-oss-120b` on Groq (`groq:openai/gpt-oss-120b`)
- Agent framework: `deepagents` (built on LangChain and LangGraph)
- Model connector: `langchain-groq`
- UI: Streamlit
- Display: pandas, mistune. Config: python-dotenv
- Optional tracing: LangSmith (set keys in `.env`)

## Next phases
- **Event-based agents:** connect a third-party app (such as Jira) so a schedule or a manual trigger starts the review and verification automatically
- **Self-improving agents:** find patterns across many saved runs and propose improvements to the agent instructions
- **Login and personal keys:** let each user sign in and enter their own Groq key instead of the shared one

## Setup (local)
1. Unzip and open a terminal in the project folder
2. `python3 -m venv .venv`
3. `source .venv/bin/activate` (Windows: `.venv\Scripts\activate`)
4. `pip install -r requirements.txt`
5. `cp .env.example .env` (Windows: `copy .env.example .env`)
6. Open `.env` and set `GROQ_API_KEY=` to your key from console.groq.com/keys

## Usage
1. `streamlit run app.py`
2. Open the local URL it prints (usually http://localhost:8501)
3. Pick a Jira or choose "New Jira...", edit the text, click **Run deep agent**
4. Read the Report, Trace, Previous run and AGENTS.md tabs
5. Click **Check against rubric**, then **Run verification loop**

## Hosting for free
- **GitHub Codespaces:**
  - Push the project to GitHub, then Code > Codespaces > Create
  - Setup runs automatically (`.devcontainer/`). Add your key to `.env`, then open the forwarded port 8501
  - Each user uses their own free Codespaces hours
- **Streamlit Community Cloud:**
  - Deploy the GitHub repo at share.streamlit.io with `app.py` as the main file
  - Add `GROQ_API_KEY` in the app's Secrets settings (see `.streamlit/secrets.toml.example`)
  - Everyone who opens the link shares one key and its limits

## Files
- `app.py` screen, `core.py` run logic (reusable by schedulers and webhooks)
- `deepagent_helpers.py`, `visualization_helpers.py` agent and display helpers
- `rubric.md`, `workspace/memories/AGENTS.md`, `jira_tickets.json` rubric, instructions and tickets
- `.devcontainer/` Codespaces setup

## Notes
- Groq's free plan allows 8,000 tokens per minute for this model, and one agent turn can exceed that. Adding a payment method (Developer plan) avoids the limit; a typical run costs cents
- Python 3.10 or newer is required
