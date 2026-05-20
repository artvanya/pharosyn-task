"""
Streaming agentic loop over ClinicalTrials.gov tools.

Yields a stream of strings. Two kinds:
  • Plain text  — the model's response tokens, yield directly to the UI.
  • Sentinel    — JSON line prefixed with \\x00, carries structured metadata:
      \\x00{"type":"tool_call",   "name":"...", "input":{...}}
      \\x00{"type":"tool_result", "name":"...", "data":{...}, "error":null}
      \\x00{"type":"error",       "message":"..."}
      \\x00{"type":"done"}
"""

import json
import os
import time
import anthropic
from dotenv import dotenv_values

from clienttrials import ClinicalTrialsClient

MODEL = "claude-sonnet-4-6"
MAX_TOOL_RETRIES = 2
RETRY_DELAY = 1.5

_anthropic = anthropic.Anthropic(
    api_key=os.environ.get("API_KEY") or dotenv_values(".env").get("API_KEY")
)
_trials = ClinicalTrialsClient()


# ── system prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a clinical trials assistant powered by ClinicalTrials.gov.

DATA INTEGRITY — ethical requirement:
- Never invent, assume, or extrapolate any information not returned by a tool.
- Every trial you mention MUST cite its NCT ID so the user can independently verify it.
- If a field is null or missing, say "not specified" — do not fill it in.
- If no results are found, say so clearly and suggest a refined search.

PIPELINE CROSS-REFERENCE:
- Pipeline verification is automatic — each study result already contains pipeline_match
  (list of matched interventions, or null if sponsor is not a tracked company) and pipeline_url.
- Always report pipeline status for each trial where pipeline_url is set:
  • pipeline_match is a non-empty list → "✓ on pipeline: <matches> — [source](<pipeline_url>)"
  • pipeline_match is an empty list    → "✗ not found on pipeline — [checked](<pipeline_url>)"
  • pipeline_match is null             → say nothing about pipeline for that trial (do NOT mention it is unavailable, not applicable, or not tracked)

TOOL USE:
- Always call a tool before answering questions about specific trials.
- Phase is NOT a search filter — search broadly and present only matching phases from results.
- get_study returns deeper detail (sponsor, endpoints, eligibility) than search_trials.
- Limit yourself to 3 tool calls per turn.

RESPONSE FORMAT:
- Be concise — aim for under 20 lines.
- Use bullet points for trial lists.
- Format each trial as: **Title** — [NCT…](url) | Status | Phase"""


# ── tool schemas ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_trials",
        "description": (
            "Search ClinicalTrials.gov for studies. Returns normalized records with "
            "nct_id, title, status, phase, conditions, interventions, lead_sponsor, "
            "primary_outcomes, and summary. Phase is NOT a direct filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "condition":    {"type": "string", "description": "Condition or disease (e.g. 'obesity')"},
                "term":         {"type": "string", "description": "General keyword search"},
                "intervention": {"type": "string", "description": "Drug or treatment name"},
                "status": {
                    "type": "string",
                    "description": "Recruitment status filter",
                    "enum": [
                        "ACTIVE_NOT_RECRUITING", "COMPLETED", "ENROLLING_BY_INVITATION",
                        "NOT_YET_RECRUITING", "RECRUITING", "SUSPENDED", "TERMINATED",
                        "WITHDRAWN", "AVAILABLE", "NO_LONGER_AVAILABLE",
                        "TEMPORARILY_NOT_AVAILABLE", "APPROVED_FOR_MARKETING",
                        "WITHHELD", "UNKNOWN",
                    ],
                },
                "page_size": {"type": "integer", "description": "Results to return (1–50, default 10)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_study",
        "description": (
            "Fetch full detail for a single trial by NCT ID: sponsor, primary endpoints, "
            "eligibility criteria, interventions, and summary. "
            "Use when the user asks about a specific NCT ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nct_id": {"type": "string", "description": "NCT ID, e.g. NCT06973720"},
            },
            "required": ["nct_id"],
        },
    },
    {
        "name": "check_company_pipeline",
        "description": (
            "Fetch a pharma company's public pipeline page and return drug/programme names. "
            "Cross-references trial interventions against the pipeline. "
            "Supported: 'novo nordisk', 'eli lilly', 'astrazeneca', 'pfizer', 'roche', 'merck', 'sanofi'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company name (lowercase)"},
                "interventions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Trial intervention names to cross-reference",
                },
            },
            "required": ["company"],
        },
    },
]


# ── pipeline annotation ───────────────────────────────────────────────────────

def _annotate_pipeline(study: dict) -> None:
    """In-place: add pipeline_match and pipeline_url to a normalized study dict."""
    from pipeline import fetch_pipeline, PIPELINE_URLS
    sponsor = (study.get("lead_sponsor") or "").lower().strip()
    if sponsor not in PIPELINE_URLS:
        study["pipeline_match"] = None
        study["pipeline_url"] = None
        return
    pipeline = fetch_pipeline(sponsor)
    matches = [
        iv for iv in study.get("interventions", [])
        if any(iv.lower() in p or p in iv.lower() for p in pipeline)
    ]
    study["pipeline_match"] = matches
    study["pipeline_url"] = PIPELINE_URLS[sponsor]


# ── tool execution ────────────────────────────────────────────────────────────

def _run_tool(name: str, tool_input: dict) -> tuple[dict, str | None]:
    """Execute a named tool, retrying on transient errors. Returns (result, error)."""
    last_err = None
    for attempt in range(MAX_TOOL_RETRIES):
        try:
            if name == "search_trials":
                raw = _trials.search_trials(
                    condition=tool_input.get("condition"),
                    term=tool_input.get("term"),
                    intervention=tool_input.get("intervention"),
                    status=tool_input.get("status"),
                    page_size=tool_input.get("page_size", 10),
                )
                if "error" in raw:
                    return {}, raw["error"]
                result = _trials.normalize_search_results(raw)
                for study in result.get("studies", []):
                    _annotate_pipeline(study)
                return result, None

            elif name == "get_study":
                raw = _trials.get_study(tool_input["nct_id"])
                if "error" in raw:
                    return {}, raw["error"]
                study = _trials.normalize_study(raw)
                _annotate_pipeline(study)
                return study, None

            elif name == "check_company_pipeline":
                from pipeline import fetch_pipeline, PIPELINE_URLS
                company = tool_input.get("company", "").lower()
                interventions = tool_input.get("interventions", [])
                pipeline_terms = fetch_pipeline(company)
                matches = [
                    iv for iv in interventions
                    if any(iv.lower() in p or p in iv.lower() for p in pipeline_terms)
                ]
                return {
                    "company": company,
                    "pipeline_terms_found": len(pipeline_terms),
                    "intervention_matches": matches,
                    "pipeline_url": PIPELINE_URLS.get(company, "not listed"),
                }, None

            else:
                return {}, f"Unknown tool: {name}"

        except Exception as exc:
            last_err = str(exc)
            if attempt < MAX_TOOL_RETRIES - 1:
                time.sleep(RETRY_DELAY)

    return {}, last_err or "Unknown error"


# ── sentinel helper ───────────────────────────────────────────────────────────

def _sentinel(obj: dict) -> str:
    return "\x00" + json.dumps(obj) + "\n"


# ── streaming agent loop ──────────────────────────────────────────────────────

def run_agent(user_message: str, history: list[dict]):
    """
    Generator yielding text chunks and \\x00-prefixed sentinel lines.
    history should contain only role=user/assistant dicts (plain text content).
    """
    messages = [m for m in history if m["role"] in ("user", "assistant")]
    messages.append({"role": "user", "content": user_message})

    while True:
        # ── stream text tokens ──────────────────────────────────────────────
        with _anthropic.messages.stream(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for text_chunk in stream.text_stream:
                yield text_chunk
            final_msg = stream.get_final_message()

        # ── check for tool use ──────────────────────────────────────────────
        tool_uses = [b for b in final_msg.content if b.type == "tool_use"]

        if not tool_uses:
            break   # pure text response — done

        # Announce tool calls
        for tu in tool_uses:
            yield _sentinel({"type": "tool_call", "name": tu.name, "input": dict(tu.input)})

        # Execute tools and collect results for next turn
        tool_result_blocks = []
        for tu in tool_uses:
            result, error = _run_tool(tu.name, dict(tu.input))
            yield _sentinel({"type": "tool_result", "name": tu.name,
                             "data": result, "error": error})

            if error:
                content = json.dumps({
                    "error": error,
                    "note": "Tool call failed after retries. Inform the user there was a data retrieval problem and suggest they retry.",
                })
            else:
                content = json.dumps(result)

            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": content,
            })

        # Append assistant turn (with tool_use blocks) + tool results
        messages.append({"role": "assistant", "content": list(final_msg.content)})
        messages.append({"role": "user", "content": tool_result_blocks})

        if final_msg.stop_reason != "tool_use":
            break

    yield _sentinel({"type": "done"})
