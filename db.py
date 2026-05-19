import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, String, Text, DateTime, Integer, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_PATH = Path(__file__).parent / "conversations.db"
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(String, primary_key=True)
    title = Column(String, default="New conversation")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    messages = relationship("Message", back_populates="conversation",
                            order_by="Message.created_at", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String, ForeignKey("conversations.id"), nullable=False)
    role = Column(String, nullable=False)        # user | assistant | tool_call | tool_result
    content = Column(Text, nullable=False)
    tool_name = Column(String, nullable=True)    # set for tool_call / tool_result rows
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    conversation = relationship("Conversation", back_populates="messages")


class PipelineCache(Base):
    __tablename__ = "pipeline_cache"
    company = Column(String, primary_key=True)
    data = Column(Text, nullable=False)         # JSON list of drug/programme names
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(engine)


# ── helpers ──────────────────────────────────────────────────────────────────

def create_conversation(conv_id: str, title: str = "New conversation") -> Conversation:
    with SessionLocal() as s:
        conv = Conversation(id=conv_id, title=title)
        s.add(conv)
        s.commit()
        s.refresh(conv)
        return conv


def get_conversation(conv_id: str) -> Conversation | None:
    with SessionLocal() as s:
        return s.get(Conversation, conv_id)


def list_conversations(limit: int = 50) -> list[dict]:
    with SessionLocal() as s:
        rows = (s.query(Conversation)
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
                .all())
        return [{"id": r.id, "title": r.title, "updated_at": r.updated_at} for r in rows]


def add_message(conv_id: str, role: str, content: str,
                tool_name: str | None = None) -> Message:
    with SessionLocal() as s:
        msg = Message(conversation_id=conv_id, role=role,
                      content=content, tool_name=tool_name)
        s.add(msg)
        # bump conversation timestamp
        conv = s.get(Conversation, conv_id)
        if conv:
            conv.updated_at = datetime.now(timezone.utc)
        s.commit()
        s.refresh(msg)
        return msg


def get_messages(conv_id: str) -> list[dict]:
    with SessionLocal() as s:
        msgs = (s.query(Message)
                .filter(Message.conversation_id == conv_id)
                .order_by(Message.created_at)
                .all())
        return [{"role": m.role, "content": m.content,
                 "tool_name": m.tool_name, "created_at": m.created_at}
                for m in msgs]


def update_conversation_title(conv_id: str, title: str) -> None:
    with SessionLocal() as s:
        conv = s.get(Conversation, conv_id)
        if conv:
            conv.title = title[:80]
            s.commit()


def get_pipeline_cache(company: str) -> list[str] | None:
    from datetime import timedelta
    with SessionLocal() as s:
        row = s.get(PipelineCache, company.lower())
        if row is None:
            return None
        age = datetime.now(timezone.utc) - row.fetched_at.replace(tzinfo=timezone.utc)
        if age > timedelta(hours=24):
            return None
        return json.loads(row.data)


def set_pipeline_cache(company: str, programmes: list[str]) -> None:
    with SessionLocal() as s:
        existing = s.get(PipelineCache, company.lower())
        if existing:
            existing.data = json.dumps(programmes)
            existing.fetched_at = datetime.now(timezone.utc)
        else:
            s.add(PipelineCache(company=company.lower(),
                                data=json.dumps(programmes)))
        s.commit()
