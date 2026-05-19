# Clinical Trials Assistant

A chatbot that answers natural language questions about clinical trials, backed by the [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api).

## Architecture

```
Streamlit UI  (app.py, port 8501)
     │  HTTP + SSE
     ▼
FastAPI backend  (api.py, port 8000)
     │
     ├── agent.py          Streaming Anthropic agentic loop
     ├── clienttrials.py   ClinicalTrials.gov API client + normaliser
     ├── pipeline.py       Company pipeline cross-reference (cached 24 h)
     └── db.py             SQLite persistence (conversations, messages, pipeline cache)
```

**Key design decisions:**
- FastAPI serves a streaming SSE endpoint; Streamlit consumes it token-by-token so users see the response as it's generated.
- Every tool call and result is stored in SQLite, giving a full auditable trail of agent reasoning.
- Pipeline pages are scraped once per 24 hours and cached in SQLite — no JS runtime required.
- Conversation ID is kept in the URL query param (`?conv=<id>`) so browser refresh restores the session.

## Setup

### 1. Clone and create virtualenv

```bash
git clone <repo-url> && cd pharosyn-task
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Add your Anthropic API key

```bash
echo "API_KEY=sk-ant-..." > .env
```

### 3. Run

Open **two terminals** (both with the venv activated):

**Terminal 1 — FastAPI backend:**
```bash
uvicorn api:app --port 8000 --reload
```

**Terminal 2 — Streamlit frontend:**
```bash
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501).

## Features

| Feature | Detail |
|---|---|
| **Streaming** | Tokens stream as they are generated; tool calls show a live status indicator |
| **Tool use** | `search_trials`, `get_study`, `check_company_pipeline` |
| **Pipeline cross-reference** | Checks Novo Nordisk, Eli Lilly, AstraZeneca, Pfizer, Roche, Merck, Sanofi |
| **Full dataset access** | Expandable dataframe below every search result; CSV download |
| **Conversation history** | Sidebar lists all past conversations; click to restore |
| **Excel export** | Download any conversation as `.xlsx` from the sidebar |
| **Session resumption** | Conversation ID in URL — browser refresh restores context |
| **Persistence** | SQLite: conversations, messages, tool traces, pipeline cache |
| **Error handling** | Tool failures retry twice; errors surfaced as inline warnings |
| **Data integrity** | System prompt enforces NCT ID citation and no hallucination |

## Example questions

- *What trials are currently recruiting for obesity?*
- *Tell me more about NCT06973720 — who is the sponsor and what's the primary endpoint?*
- *Are there any trials comparing semaglutide to placebo for obesity?*
- *What are Novo Nordisk's current active trials for obesity?*

## Eval

```bash
python eval.py
```

Runs 4 test cases using **LLM-as-judge** (Claude scores each response against a rubric). Results are printed and saved to `eval_results.json`.

**Rubric covers:** NCT ID presence, correct sponsor attribution, status accuracy, no fabricated data.

**Known eval note:** The judge may flag `NCT07XXXXXX` IDs as suspicious — these are real 2025–26 registrations that post-date the judge's training cutoff. The IDs are sourced directly from the live API and are not invented.

## File structure

```
api.py            FastAPI backend (SSE streaming, REST endpoints)
agent.py          Streaming agentic loop with tool execution and retry
app.py            Streamlit chat UI
clienttrials.py   ClinicalTrials.gov client — search_trials, get_study, normalize_study
pipeline.py       Pipeline page scraper and cross-referencer
db.py             SQLAlchemy models + CRUD helpers
eval.py           LLM-as-judge eval suite
requirements.txt  Python dependencies
.env              API key (not committed)
```
