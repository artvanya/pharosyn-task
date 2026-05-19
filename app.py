"""
Streamlit frontend.  Communicates with the FastAPI backend via HTTP.

Session resumption: the active conversation ID is kept in st.query_params
so a browser refresh restores the same conversation from the DB.
"""

import json
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


# ── session state bootstrap ───────────────────────────────────────────────────

# Restore conversation from URL param on refresh
if "conv_id" not in st.session_state:
    url_conv = st.query_params.get("conv")
    if url_conv:
        # Verify it exists in DB before trusting it
        existing = api("get", f"/api/conversations/{url_conv}/messages")
        st.session_state.conv_id = url_conv if existing is not None else None
    else:
        st.session_state.conv_id = None

if "messages" not in st.session_state:
    st.session_state.messages = []   # list of {role, content, meta}

if "full_data" not in st.session_state:
    st.session_state.full_data = {}  # msg_index → list[dict] of trial data


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
            st.session_state.full_data = {}
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
        # Show full dataset expander if this message has trial data attached
        if msg["role"] == "assistant" and i in st.session_state.full_data:
            studies = st.session_state.full_data[i]
            if studies:
                with st.expander(f"📋 Full dataset — {len(studies)} trials"):
                    df = pd.DataFrame(studies)
                    st.dataframe(df, use_container_width=True)
                    csv = df.to_csv(index=False).encode()
                    st.download_button("⬇ Download CSV", csv,
                                       file_name="trials.csv", mime="text/csv",
                                       key=f"csv_{i}")

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
                    payload = line[6:]  # strip "data: "

                    if payload.startswith("\x00"):
                        # Sentinel
                        sentinel = json.loads(payload[1:])
                        stype = sentinel["type"]

                        if stype == "tool_call":
                            tool_status.info(
                                f"🔍 Calling **{sentinel['name']}**…",
                                icon="⏳"
                            )

                        elif stype == "tool_result":
                            tool_status.empty()
                            if sentinel.get("error"):
                                st.warning(
                                    f"⚠ Tool **{sentinel['name']}** failed: {sentinel['error']}",
                                )
                            else:
                                # Collect trial data for the full-dataset expander
                                data = sentinel.get("data", {})
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

        msg_index = len(st.session_state.messages)
        st.session_state.messages.append({"role": "assistant", "content": text_so_far})

        # Attach full trial data to this message for the expander
        if collected_studies:
            st.session_state.full_data[msg_index] = collected_studies
            with st.expander(f"📋 Full dataset — {len(collected_studies)} trials"):
                df = pd.DataFrame(collected_studies)
                st.dataframe(df, use_container_width=True)
                csv = df.to_csv(index=False).encode()
                st.download_button("⬇ Download CSV", csv,
                                   file_name="trials.csv", mime="text/csv",
                                   key=f"csv_new")
