# Clinical Trials Assistant

A conversational agent that answers natural-language questions about clinical trials, backed by the live [ClinicalTrials.gov API v2](https://clinicaltrials.gov/data-api/api).

**Live demo:** https://pharosyn-task.onrender.com  
*(Free-tier host — first load after inactivity takes ~30 s to warm up)*

---

## Architecture

```
Browser
  │  WebSocket
  ▼
Streamlit UI  (app.py, port 8501)
  │  HTTP + Server-Sent Events
  ▼
FastAPI backend  (api.py, port 8000)
  │
  ├── agent.py          Streaming Anthropic agentic loop
  ├── clienttrials.py   ClinicalTrials.gov API client + normaliser
  ├── pipeline.py       Company pipeline cross-reference (cached 24 h)
  └── db.py             SQLAlchemy / SQLite persistence
```

### Key design decisions

**Background thread for resilience.** The agent runs in a `threading.Thread` inside the FastAPI process. Even if the browser disconnects mid-stream, the thread finishes, persists the full answer to SQLite, and the next page load restores it from the DB. This satisfies the "refresh while thinking" requirement without any client-side service workers or websocket reconnect logic.

**SSE → Streamlit → browser.** FastAPI streams newline-delimited SSE. Every chunk (text token or structured sentinel) is JSON-encoded before transmission so markdown newlines survive `iter_lines()` without corruption. Sentinels are prefixed with `\x00` to distinguish them from text tokens.

**SQLite for everything.** Conversations, messages, tool traces (tool_call / tool_result rows), and pipeline cache all live in one SQLite file. Tool traces give a full auditable trail of every agent reasoning step, queryable long after the conversation ends.

**Pipeline caching.** Pipeline pages are fetched once per 24 hours and stored as JSON in SQLite. Re-fetching on every request would be slow and impolite to company servers; 24 hours is a reasonable TTL for pipeline data that changes on a scale of weeks.

---

## Features

| Feature | Detail |
|---|---|
| **Streaming** | Tokens appear as the LLM generates them; tool calls show a live "Calling…" indicator |
| **Tool use** | `search_trials`, `get_study` — agent decides which to call and when |
| **Pipeline cross-reference** | Every trial's lead sponsor is checked against 7 major pharma pipeline pages; result cited per trial |
| **Agent reasoning** | Collapsible "🔍 Agent reasoning" expander per response — shows tool inputs and result counts |
| **Full dataset** | Expandable dataframe below search results; CSV and Excel download |
| **Conversation history** | Sidebar lists all conversations; click to restore full context |
| **Excel export** | Download any conversation as `.xlsx` from the sidebar |
| **Session resilience** | Conversation ID in URL (`?conv=<id>`) — browser refresh mid-stream restores context |
| **Persistence** | SQLite: conversations, messages, tool traces, pipeline cache |
| **Error handling** | Tool failures retry twice with back-off; errors surfaced as inline warnings |

---

## Pipeline cross-reference: approach and trade-offs

**Goal:** for every trial returned, verify whether the drug/intervention appears on the lead sponsor's public pipeline page, and cite the source.

**Approach:** fetch the pipeline page's raw HTML and apply regex patterns for INN drug suffixes (`-mab`, `-glutide`, `-lintide`, `-nib`, etc.) and internal compound codes (`NNC-1234`, `LY-5678`). Results are cached for 24 hours.

**Why regex on raw HTML rather than a browser / headless Chrome?**  
Most pharma pipeline pages are React or AEM apps — the pipeline table is rendered client-side. A full JS runtime would require Playwright/Puppeteer, adding significant deployment complexity and memory. In practice, AEM sites embed drug names in the HTML source as data attributes or SSR-rendered text even when the visual table is JS-drawn, so raw-HTML extraction is surprisingly effective. Approved/marketed drugs (e.g. semaglutide at Novo Nordisk) may not appear on the *investigational* pipeline page at all — which is correct behaviour, not a bug.

**Trade-offs:**
- *24 h cache* — pipeline data can be stale by up to a day. Acceptable given how infrequently pipeline pages update; reduces latency on subsequent requests from ~15 s to <1 ms.
- *No JS rendering* — compounds listed only in dynamically-loaded tables will be missed. The response always cites the pipeline URL so the user can verify directly.
- *Fuzzy matching* — intervention names from trials ("Semaglutide 2.4 mg injection") are normalised and compared as substrings against extracted pipeline terms. This catches partial matches but could produce false positives for very short names.

**Data validity:** ClinicalTrials.gov is an FDA-regulated registry — sponsors are legally required to keep records current. Trial status, sponsor, and endpoints come directly from the registry API with no intermediate transformation beyond field normalisation. The agent's system prompt enforces strict no-hallucination rules: every claim must come from a tool result.

---

## Setup

### 1. Clone and create virtualenv

```bash
git clone https://github.com/artvanya/pharosyn-task && cd pharosyn-task
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

```bash
# Terminal 1
uvicorn api:app --port 8000 --reload

# Terminal 2
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501).

---

## Eval

```bash
python eval.py
```

Runs 4 test cases with **LLM-as-judge** (Claude grades each response against a rubric). Results printed to console and saved to `eval_results.json`.

**Latest results: 11/13 checks passed (84.6%)**

| Test | Result |
|---|---|
| `obesity_recruiting` | PARTIAL 2/3 |
| `trial_detail` | PASS 3/3 |
| `semaglutide_placebo` | PARTIAL 3/4 |
| `novo_nordisk_obesity` | PASS 3/3 |

The two partial passes are cases where the judge flags `NCT07XXXXXX` IDs as potentially fabricated — these are real 2025–26 registrations from the live API that post-date the judge model's training cutoff. The IDs are verifiable on ClinicalTrials.gov directly.

**Rubric covers:** NCT ID presence, correct sponsor attribution, trial status accuracy, no fabricated data, semaglutide/placebo comparator arm mentioned.

---

## File structure

```
api.py            FastAPI backend — SSE streaming, REST endpoints
agent.py          Streaming agentic loop, tool execution with retry, system prompt
app.py            Streamlit chat UI — streaming, session restore, expanders
clienttrials.py   ClinicalTrials.gov client — search, get_study, normalise
pipeline.py       Pipeline page scraper, regex extraction, 24 h cache
db.py             SQLAlchemy models + CRUD helpers
eval.py           LLM-as-judge eval suite
start.sh          Production startup script (FastAPI + Streamlit in one container)
railway.toml      Railway deployment config
requirements.txt  Python dependencies
.env              API key (not committed)
```
