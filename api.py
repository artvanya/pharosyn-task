"""
FastAPI backend.

POST /api/conversations                 — create a new conversation
GET  /api/conversations                 — list all conversations (sidebar)
GET  /api/conversations/{id}/messages   — full message history for a conversation
GET  /api/conversations/{id}/export     — download conversation as Excel
POST /api/conversations/{id}/stream     — SSE stream for a chat turn

The agent runs in a background thread so it completes even if the browser
disconnects mid-stream.  On reload the completed answer is waiting in the DB.
"""

import io
import json
import queue
import threading
import uuid

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db
from agent import run_agent

app = FastAPI(title="Clinical Trials Assistant API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_DONE = object()   # sentinel to signal end-of-stream from background thread


# ── request / response models ─────────────────────────────────────────────────

class NewConversationRequest(BaseModel):
    title: str = "New conversation"


class ChatRequest(BaseModel):
    message: str


# ── conversation endpoints ────────────────────────────────────────────────────

@app.post("/api/conversations", status_code=201)
def create_conversation(req: NewConversationRequest):
    conv_id = str(uuid.uuid4())
    db.create_conversation(conv_id, req.title)
    return {"id": conv_id, "title": req.title}


@app.get("/api/conversations")
def list_conversations():
    return db.list_conversations()


@app.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: str):
    if not db.get_conversation(conv_id):
        raise HTTPException(404, "Conversation not found")
    return db.get_messages(conv_id)


@app.get("/api/conversations/{conv_id}/export")
def export_conversation(conv_id: str):
    if not db.get_conversation(conv_id):
        raise HTTPException(404, "Conversation not found")
    messages = db.get_messages(conv_id)
    rows = [
        {
            "role": m["role"],
            "content": m["content"],
            "tool": m.get("tool_name") or "",
            "timestamp": m["created_at"].isoformat() if m.get("created_at") else "",
        }
        for m in messages
        if m["role"] in ("user", "assistant")
    ]
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Conversation")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="conversation_{conv_id[:8]}.xlsx"'},
    )


# ── streaming chat endpoint ───────────────────────────────────────────────────

@app.post("/api/conversations/{conv_id}/stream")
def chat_stream(conv_id: str, req: ChatRequest):
    if not db.get_conversation(conv_id):
        raise HTTPException(404, "Conversation not found")

    # Persist user message and snapshot history before the thread starts
    db.add_message(conv_id, "user", req.message)
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in db.get_messages(conv_id)
        if m["role"] in ("user", "assistant")
    ][:-1]   # exclude the message we just added; agent adds it itself

    chunk_queue: queue.Queue = queue.Queue()

    def _agent_thread():
        """
        Runs the agent to completion regardless of whether the SSE client
        is still connected.  All DB writes happen here so nothing is lost
        on browser disconnect.
        """
        full_response: list[str] = []
        try:
            for chunk in run_agent(req.message, history):
                chunk_queue.put(chunk)

                if chunk.startswith("\x00"):
                    sentinel = json.loads(chunk[1:])
                    if sentinel["type"] == "tool_call":
                        db.add_message(conv_id, "tool_call",
                                       json.dumps(sentinel["input"]),
                                       tool_name=sentinel["name"])
                    elif sentinel["type"] == "tool_result":
                        db.add_message(conv_id, "tool_result",
                                       json.dumps({"data": sentinel["data"],
                                                   "error": sentinel["error"]}),
                                       tool_name=sentinel["name"])
                else:
                    full_response.append(chunk)

        except Exception as exc:
            chunk_queue.put(
                "\x00" + json.dumps({"type": "error", "message": str(exc)}) + "\n"
            )
        finally:
            # Persist completed (or partial) assistant text to DB.
            # This runs even if the SSE client already disconnected.
            if full_response:
                assistant_text = "".join(full_response)
                db.add_message(conv_id, "assistant", assistant_text)
                all_msgs = db.get_messages(conv_id)
                if sum(1 for m in all_msgs if m["role"] == "user") == 1:
                    title = req.message[:60] + ("…" if len(req.message) > 60 else "")
                    db.update_conversation_title(conv_id, title)
            chunk_queue.put(_DONE)

    thread = threading.Thread(target=_agent_thread, daemon=True)
    thread.start()

    def event_generator():
        """
        Reads chunks from the queue and forwards them to the SSE client.
        If the client disconnects (GeneratorExit), we just stop reading —
        the background thread keeps running and saves to DB on its own.
        """
        while True:
            try:
                chunk = chunk_queue.get(timeout=120)
            except queue.Empty:
                break
            if chunk is _DONE:
                break
            # JSON-encode so newlines inside chunks are escaped (\n → \\n)
            # and iter_lines() on the client doesn't split mid-chunk.
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
