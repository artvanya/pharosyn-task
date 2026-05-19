"""
FastAPI backend.

POST /api/conversations                 — create a new conversation
GET  /api/conversations                 — list all conversations (sidebar)
GET  /api/conversations/{id}/messages   — full message history for a conversation
GET  /api/conversations/{id}/export     — download conversation as Excel
POST /api/conversations/{id}/stream     — SSE stream for a chat turn

The SSE stream sends lines of text.  Two formats:
  data: <text chunk>\n\n          — raw assistant token(s)
  data: \x00{...json...}\n\n      — sentinel (tool_call / tool_result / done / error)
"""

import io
import json
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

    def event_generator():
        # Persist user message
        db.add_message(conv_id, "user", req.message)

        # Build plain-text history for the agent
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in db.get_messages(conv_id)
            if m["role"] in ("user", "assistant")
        ]
        # Remove the message we just added (agent adds it itself)
        history = history[:-1]

        full_response = []

        try:
            for chunk in run_agent(req.message, history):
                if chunk.startswith("\x00"):
                    # Sentinel — parse and persist tool traces, then forward
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
                    yield f"data: {chunk}\n\n"
                else:
                    full_response.append(chunk)
                    yield f"data: {chunk}\n\n"

        except Exception as exc:
            error_sentinel = "\x00" + json.dumps({"type": "error", "message": str(exc)}) + "\n"
            yield f"data: {error_sentinel}\n\n"

        # Persist complete assistant response
        if full_response:
            assistant_text = "".join(full_response)
            db.add_message(conv_id, "assistant", assistant_text)
            # Auto-title conversation from first user message
            messages = db.get_messages(conv_id)
            user_msgs = [m for m in messages if m["role"] == "user"]
            if len(user_msgs) == 1:
                title = req.message[:60] + ("…" if len(req.message) > 60 else "")
                db.update_conversation_title(conv_id, title)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
