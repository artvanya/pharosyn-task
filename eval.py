"""
Eval suite for the Clinical Trials Assistant.

Approach: LLM-as-judge.
Each test case defines a user question and a rubric of required properties.
We run the agent, collect the full response, then ask Claude to score it
against the rubric.  Results are printed as a pass/fail table and saved
to eval_results.json.

Run:
    .venv/bin/python eval.py
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import os
import anthropic
from dotenv import dotenv_values

from agent import run_agent

_client = anthropic.Anthropic(
    api_key=os.environ.get("API_KEY") or dotenv_values(".env").get("API_KEY")
)

JUDGE_MODEL = "claude-sonnet-4-6"


# ── test cases ────────────────────────────────────────────────────────────────

@dataclass
class TestCase:
    name: str
    question: str
    rubric: list[str]           # things the response MUST satisfy
    history: list[dict] = field(default_factory=list)


TEST_CASES = [
    TestCase(
        name="obesity_recruiting",
        question="What trials are currently recruiting for obesity?",
        rubric=[
            "Response contains at least one NCT ID (pattern NCT followed by digits)",
            "At least one trial has status RECRUITING or 'currently recruiting' mentioned",
            "Response does not invent data — only mentions trials with real NCT IDs",
        ],
    ),
    TestCase(
        name="trial_detail",
        question="Tell me more about NCT06973720 — who is the sponsor and what's the primary endpoint?",
        rubric=[
            "Response mentions the sponsor or lead organisation of NCT06973720",
            "Response mentions at least one primary endpoint or outcome measure",
            "Response cites NCT06973720 explicitly",
        ],
    ),
    TestCase(
        name="semaglutide_placebo",
        question="Are there any trials comparing semaglutide to placebo for obesity?",
        rubric=[
            "Response includes at least one NCT ID",
            "Response mentions semaglutide",
            "Response mentions placebo or comparator arm",
            "Response does not fabricate trial details",
        ],
    ),
    TestCase(
        name="novo_nordisk_obesity",
        question="What are Novo Nordisk's current active trials for obesity?",
        rubric=[
            "Response mentions Novo Nordisk as a sponsor",
            "Response includes at least one NCT ID",
            "Response describes the trial status (recruiting, active, etc.)",
        ],
    ),
]


# ── agent runner ──────────────────────────────────────────────────────────────

def collect_response(question: str, history: list[dict]) -> tuple[str, list[str]]:
    """Run the agent and return (full_text, list_of_tool_names_called)."""
    text_parts = []
    tools_called = []
    for chunk in run_agent(question, history):
        if chunk.startswith("\x00"):
            s = json.loads(chunk[1:])
            if s["type"] == "tool_call":
                tools_called.append(s["name"])
        else:
            text_parts.append(chunk)
    return "".join(text_parts), tools_called


# ── LLM judge ────────────────────────────────────────────────────────────────

JUDGE_SYSTEM = """You are an evaluator for a clinical trials chatbot.
You are given a user question, the chatbot's response, and a rubric.
For each rubric item, decide if the response satisfies it.
Reply with a JSON object:
{
  "scores": [true, false, ...],   // one boolean per rubric item, same order
  "reasoning": ["...", "..."]     // one short explanation per rubric item
}
Only output valid JSON, nothing else."""


def judge(question: str, response: str, rubric: list[str]) -> dict:
    prompt = f"""Question: {question}

Response:
{response}

Rubric:
{json.dumps(rubric, indent=2)}"""

    msg = _client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=1024,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ── run suite ─────────────────────────────────────────────────────────────────

def run_eval():
    results = []
    total_pass = 0
    total_checks = 0

    print(f"\n{'='*60}")
    print("  Clinical Trials Assistant — Eval Suite")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    for tc in TEST_CASES:
        print(f"▶ {tc.name}")
        print(f"  Q: {tc.question}")

        t0 = time.time()
        response, tools = collect_response(tc.question, tc.history)
        elapsed = time.time() - t0
        print(f"  Tools called: {tools}  ({elapsed:.1f}s)")

        verdict = judge(tc.question, response, tc.rubric)
        scores = verdict.get("scores", [])
        reasoning = verdict.get("reasoning", [])

        passed = sum(scores)
        total_pass += passed
        total_checks += len(scores)

        for i, (item, ok, reason) in enumerate(zip(tc.rubric, scores, reasoning)):
            icon = "✅" if ok else "❌"
            print(f"  {icon} [{i+1}] {item[:70]}")
            if not ok:
                print(f"       → {reason}")

        status = "PASS" if all(scores) else "PARTIAL" if passed else "FAIL"
        print(f"  → {status} ({passed}/{len(scores)} checks)\n")

        results.append({
            "test": tc.name,
            "question": tc.question,
            "status": status,
            "passed": passed,
            "total": len(scores),
            "tools_called": tools,
            "elapsed_s": round(elapsed, 1),
            "response_preview": response[:300],
            "rubric_detail": [
                {"item": r, "pass": s, "reason": e}
                for r, s, e in zip(tc.rubric, scores, reasoning)
            ],
        })

    overall = f"{total_pass}/{total_checks}"
    print(f"{'='*60}")
    print(f"  Overall: {overall} checks passed")
    print(f"{'='*60}\n")

    with open("eval_results.json", "w") as f:
        json.dump({
            "run_at": datetime.now().isoformat(),
            "overall": overall,
            "cases": results,
        }, f, indent=2)
    print("Results saved to eval_results.json")

    return results


if __name__ == "__main__":
    run_eval()
