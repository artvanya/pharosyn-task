"""
Streamlit frontend.  Communicates with the FastAPI backend via HTTP.

Session resumption: the active conversation ID is kept in st.query_params
so a browser refresh restores the same conversation from the DB.
"""

import json
import time
import uuid
import requests
import pandas as pd
import streamlit as st

API = "http://localhost:8000"

st.set_page_config(
    page_title="Clinical Trials Assistant",
    page_icon="🔬",
    layout="wide",
)

# ── helpers ───────────────────────────────────────────────────────────────────

def api(method: str, path: str, **kwargs):
    try:
        r = getattr(requests, method)(f"{API}{path}", timeout=10, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def load_conversations():
    return api("get", "/api/conversations") or []


def create_conversation():
    result = api("post", "/api/conversations", json={"title": "New conversation"})
    return result["id"] if result else None


def load_messages(conv_id: str):
    return api("get", f"/api/conversations/{conv_id}/messages") or []


def _render_tool_traces(traces: list) -> None:
    """Render a collapsible expander showing tool calls and results."""
    n_calls = sum(1 for t in traces if t["type"] == "tool_call")
    with st.expander(f"🔍 Agent reasoning — {n_calls} tool call{'s' if n_calls != 1 else ''}"):
        for t in traces:
            if t["type"] == "tool_call":
                inp = {k: v for k, v in t.get("input", {}).items() if v}
                st.markdown(f"**📞 `{t['name']}`** — `{inp}`")
            elif t["type"] == "tool_result":
                if t.get("error"):
                    st.warning(f"⚠ `{t['name']}` failed: {t['error']}")
                else:
                    data = t.get("data", {})
                    if "studies" in data:
                        st.markdown(f"↳ {len(data['studies'])} studies returned")
                    elif "pipeline_terms_found" in data:
                        n = data["pipeline_terms_found"]
                        matches = data.get("intervention_matches", [])
                        detail = f"matches: **{', '.join(matches)}**" if matches else "no matches"
                        st.markdown(f"↳ {n} pipeline terms · {detail}")
                    elif "nct_id" in data:
                        st.markdown(f"↳ fetched **{data['nct_id']}**")
                    else:
                        st.markdown("↳ done")


def extract_tool_traces(all_messages: list) -> tuple[dict, dict]:
    """
    Parse a full message list (all roles) into:
      tool_traces  — {assistant_msg_index: [trace_dict, ...]}
      full_data    — {assistant_msg_index: [study_dict, ...]}
    Index is the position of the message in the visible (user+assistant) list.
    """
    traces: dict = {}
    full_data: dict = {}
    pending_traces: list = []
    pending_studies: list = []
    visible_idx = -1
    for m in all_messages:
        role = m["role"]
        if role == "user":
            visible_idx += 1
            pending_traces = []
            pending_studies = []
        elif role == "tool_call":
            try:
                inp = json.loads(m["content"])
            except Exception:
                inp = {}
            pending_traces.append({"type": "tool_call", "name": m.get("tool_name", ""), "input": inp})
        elif role == "tool_result":
            try:
                body = json.loads(m["content"])
            except Exception:
                body = {}
            data = body.get("data", {})
            pending_traces.append({
                "type": "tool_result", "name": m.get("tool_name", ""),
                "data": data, "error": body.get("error"),
            })
            if "studies" in data:
                pending_studies.extend(data["studies"])
        elif role == "assistant":
            visible_idx += 1
            if pending_traces:
                traces[visible_idx] = pending_traces[:]
            if pending_studies:
                full_data[visible_idx] = pending_studies[:]
            pending_traces = []
            pending_studies = []
    return traces, full_data


# ── session state bootstrap ───────────────────────────────────────────────────

# Restore conversation from URL param on refresh
if "conv_id" not in st.session_state:
    url_conv = st.query_params.get("conv")
    if url_conv:
        existing = api("get", f"/api/conversations/{url_conv}/messages")
        if existing is not None:
            st.session_state.conv_id = url_conv
            st.session_state.messages = [
                {"role": m["role"], "content": m["content"]}
                for m in existing
                if m["role"] in ("user", "assistant")
            ]
            st.session_state.tool_traces, st.session_state.full_data = (
                extract_tool_traces(existing)
            )
            # Detect if we reloaded while the agent was still thinking:
            # last DB message is from the user with no assistant reply yet.
            visible = [m for m in existing if m["role"] in ("user", "assistant")]
            st.session_state.answer_pending = (
                bool(visible) and visible[-1]["role"] == "user"
            )
        else:
            st.session_state.conv_id = None
    else:
        st.session_state.conv_id = None

if "answer_pending" not in st.session_state:
    st.session_state.answer_pending = False

if "messages" not in st.session_state:
    st.session_state.messages = []

if "full_data" not in st.session_state:
    st.session_state.full_data = {}  # msg_index → list[dict] of trial data

if "tool_traces" not in st.session_state:
    st.session_state.tool_traces = {}  # msg_index → list[trace dicts]


# ── sidebar: conversation history ─────────────────────────────────────────────

with st.sidebar:
    st.title("🔬 Clinical Trials")

    if st.button("＋ New conversation", use_container_width=True):
        conv_id = create_conversation()
        if conv_id:
            st.session_state.conv_id = conv_id
            st.session_state.messages = []
            st.session_state.full_data = {}
            st.query_params["conv"] = conv_id
            st.rerun()

    st.divider()
    st.caption("Previous conversations")

    conversations = load_conversations()
    for conv in conversations:
        label = conv["title"]
        is_active = conv["id"] == st.session_state.conv_id
        btn_label = f"**{label}**" if is_active else label
        if st.button(btn_label, key=conv["id"], use_container_width=True):
            st.session_state.conv_id = conv["id"]
            raw_msgs = load_messages(conv["id"])
            st.session_state.messages = [
                {"role": m["role"], "content": m["content"]}
                for m in raw_msgs
                if m["role"] in ("user", "assistant")
            ]
            st.session_state.tool_traces, st.session_state.full_data = (
                extract_tool_traces(raw_msgs)
            )
            st.query_params["conv"] = conv["id"]
            st.rerun()

    st.divider()

    # Excel export for current conversation
    if st.session_state.conv_id:
        export_url = f"{API}/api/conversations/{st.session_state.conv_id}/export"
        try:
            r = requests.get(export_url, timeout=10)
            if r.status_code == 200:
                st.download_button(
                    "⬇ Export to Excel",
                    data=r.content,
                    file_name="conversation.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
        except Exception:
            pass

    st.caption("Data sourced from ClinicalTrials.gov · All trials cited by NCT ID")


# ── main chat area ─────────────────────────────────────────────────────────────

st.title("Clinical Trials Assistant")

# If no conversation yet, create one silently
if not st.session_state.conv_id:
    conv_id = create_conversation()
    if conv_id:
        st.session_state.conv_id = conv_id
        st.query_params["conv"] = conv_id

# Render history
for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            # Agent thought process expander
            if i in st.session_state.tool_traces:
                _render_tool_traces(st.session_state.tool_traces[i])
            # Full dataset expander
            if i in st.session_state.full_data:
                studies = st.session_state.full_data[i]
                if studies:
                    with st.expander(f"📋 Full dataset — {len(studies)} trials"):
                        df = pd.DataFrame(studies)
                        st.dataframe(df, use_container_width=True)
                        csv = df.to_csv(index=False).encode()
                        st.download_button("⬇ Download CSV", csv,
                                           file_name="trials.csv", mime="text/csv",
                                           key=f"csv_{i}")

if st.session_state.answer_pending:
    with st.chat_message("assistant"):
        st.info("Generating response…", icon="⏳")
        # Auto-poll every 4 s until the background thread saves the answer
        time.sleep(4)
        existing = api("get", f"/api/conversations/{st.session_state.conv_id}/messages")
        if existing:
            st.session_state.messages = [
                {"role": m["role"], "content": m["content"]}
                for m in existing
                if m["role"] in ("user", "assistant")
            ]
            st.session_state.tool_traces, st.session_state.full_data = (
                extract_tool_traces(existing)
            )
            visible = [m for m in existing if m["role"] in ("user", "assistant")]
            st.session_state.answer_pending = (
                bool(visible) and visible[-1]["role"] == "user"
            )
        st.rerun()

# Chat input
if user_input := st.chat_input("Ask about clinical trials…"):
    if not st.session_state.conv_id:
        st.error("Could not connect to backend. Is the FastAPI server running?")
        st.stop()

    # Show user message immediately
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Stream assistant response
    with st.chat_message("assistant"):
        response_placeholder = st.empty()
        tool_status = st.empty()

        text_so_far = ""
        collected_studies = []
        live_traces: list = []

        try:
            with requests.post(
                f"{API}/api/conversations/{st.session_state.conv_id}/stream",
                json={"message": user_input},
                stream=True,
                timeout=120,
            ) as r:
                r.raise_for_status()
                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8")
                    if not line.startswith("data: "):
                        continue
                    # Every SSE payload is JSON-encoded to survive newlines.
                    try:
                        payload = json.loads(line[6:])
                    except (json.JSONDecodeError, ValueError):
                        continue

                    if not isinstance(payload, str):
                        continue

                    if payload.startswith("\x00"):
                        # Sentinel
                        sentinel = json.loads(payload[1:])
                        stype = sentinel["type"]

                        if stype == "tool_call":
                            tool_status.info(
                                f"🔍 Calling **{sentinel['name']}**…",
                                icon="⏳"
                            )
                            live_traces.append({
                                "type": "tool_call",
                                "name": sentinel["name"],
                                "input": sentinel.get("input", {}),
                            })

                        elif stype == "tool_result":
                            tool_status.empty()
                            data = sentinel.get("data", {})
                            live_traces.append({
                                "type": "tool_result",
                                "name": sentinel["name"],
                                "data": data,
                                "error": sentinel.get("error"),
                            })
                            if sentinel.get("error"):
                                st.warning(
                                    f"⚠ Tool **{sentinel['name']}** failed: {sentinel['error']}",
                                )
                            else:
                                if "studies" in data:
                                    collected_studies.extend(data["studies"])

                        elif stype == "error":
                            st.error(f"Error: {sentinel['message']}")

                    else:
                        # Plain text token
                        text_so_far += payload
                        response_placeholder.markdown(text_so_far + "▌")

        except requests.exceptions.ConnectionError:
            st.error("Cannot reach the backend. Start it with: `uvicorn api:app --port 8000`")
            st.stop()
        except Exception as e:
            st.error(f"Streaming error: {e}")
            st.stop()

        # Finalise display
        tool_status.empty()
        response_placeholder.markdown(text_so_far)

        # If the proxy buffered everything and delivered nothing, fall back to
        # polling the DB (answer_pending auto-refreshes every 4 s).
        if not text_so_far:
            st.session_state.answer_pending = True
            response_placeholder.info("⏳ Generating response — updating automatically…")
            st.rerun()

        st.session_state.answer_pending = False

        msg_index = len(st.session_state.messages)
        st.session_state.messages.append({"role": "assistant", "content": text_so_far})

        # Attach tool traces and trial data to this message index
        if live_traces:
            st.session_state.tool_traces[msg_index] = live_traces
            _render_tool_traces(live_traces)
        if collected_studies:
            st.session_state.full_data[msg_index] = collected_studies
            with st.expander(f"📋 Full dataset — {len(collected_studies)} trials"):
                df = pd.DataFrame(collected_studies)
                st.dataframe(df, use_container_width=True)
                csv = df.to_csv(index=False).encode()
                st.download_button("⬇ Download CSV", csv,
                                   file_name="trials.csv", mime="text/csv",
                                   key=f"csv_new")
