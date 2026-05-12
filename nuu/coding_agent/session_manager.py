"""
JSONL-based session persistence manager. Stores messages, thinking level
changes, model changes, and compaction entries in append-only log format.

Owns: SessionManager, SessionHeader/SessionEntry models, session file I/O.
Delegates to: json for serialization, uuid for IDs.

Data flow: SessionManager.append_message() -> writes JSONL line ->
  build_session_context() -> history reconstruction for Agent initialization

Depends on: nuu.agent.types (AgentMessage), pydantic
"""

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Union, Literal
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from ..agent.types import AgentMessage


class SessionHeader(BaseModel):
    type: Literal["session"] = "session"
    version: int = 3
    id: str
    timestamp: str
    cwd: str
    parent_session: Optional[str] = None


class SessionEntryBase(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    id: str
    parent_id: Optional[str] = None
    timestamp: str


class SessionMessageEntry(SessionEntryBase):
    type: Literal["message"] = "message"
    message: AgentMessage


class ThinkingLevelChangeEntry(SessionEntryBase):
    type: Literal["thinking_level_change"] = "thinking_level_change"
    thinking_level: str


class ModelChangeEntry(SessionEntryBase):
    type: Literal["model_change"] = "model_change"
    provider: str
    model_id: str


class CompactionEntry(SessionEntryBase):
    type: Literal["compaction"] = "compaction"
    summary: str
    first_kept_entry_id: str
    tokens_before: int
    details: Optional[Any] = None


class SessionInfoEntry(SessionEntryBase):
    type: Literal["session_info"] = "session_info"
    name: Optional[str] = None


SessionEntry = Union[
    SessionMessageEntry,
    ThinkingLevelChangeEntry,
    ModelChangeEntry,
    CompactionEntry,
    SessionInfoEntry,
]

FileEntry = Union[SessionHeader, SessionEntry]


class SessionManager:
    def __init__(
        self,
        cwd: str,
        session_dir: str,
        session_file: Optional[str] = None,
        persist: bool = True,
    ):
        self.cwd = cwd
        self.session_dir = session_dir
        self.persist = persist
        self.session_file = session_file
        self.session_id = str(uuid.uuid4())
        self.entries: List[FileEntry] = []
        self.by_id: Dict[str, SessionEntry] = {}
        self.leaf_id: Optional[str] = None
        self.flushed = False

        if persist and not os.path.exists(session_dir):
            os.makedirs(session_dir, exist_ok=True)

        if session_file and os.path.exists(session_file):
            self._load_session(session_file)
        else:
            self._new_session()

    def _new_session(self):
        self.session_id = str(uuid.uuid4())
        timestamp = self._now_iso()
        header = SessionHeader(
            id=self.session_id,
            timestamp=timestamp,
            cwd=self.cwd,
        )
        self.entries = [header]
        self.by_id = {}
        self.leaf_id = None
        self.flushed = False

        if self.persist:
            file_timestamp = timestamp.replace(":", "-").replace(".", "-")
            self.session_file = os.path.join(
                self.session_dir, f"{file_timestamp}_{self.session_id}.jsonl"
            )

    def _load_session(self, path: str):
        self.entries = []
        with open(path, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("type") == "session":
                    self.entries.append(SessionHeader(**data))
                else:
                    # Generic entry loading
                    etype = data.get("type")
                    if etype == "message":
                        entry = SessionMessageEntry(**data)
                    elif etype == "thinking_level_change":
                        entry = ThinkingLevelChangeEntry(**data)
                    elif etype == "model_change":
                        entry = ModelChangeEntry(**data)
                    elif etype == "compaction":
                        entry = CompactionEntry(**data)
                    elif etype == "session_info":
                        entry = SessionInfoEntry(**data)
                    else:
                        continue
                    self.entries.append(entry)
                    self.by_id[entry.id] = entry
                    self.leaf_id = entry.id

        header = next((e for e in self.entries if isinstance(e, SessionHeader)), None)
        if header:
            self.session_id = header.id
        self.flushed = True

    def _now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _generate_id(self) -> str:
        return str(uuid.uuid4())[:8]

    def _append_entry(self, entry: SessionEntry):
        self.entries.append(entry)
        self.by_id[entry.id] = entry
        self.leaf_id = entry.id
        self._persist(entry)

    def _persist(self, entry: SessionEntry):
        if not self.persist or not self.session_file:
            return

        # Simplified persistence: always append if possible
        # In TS, it waits for an assistant message to flush. I'll keep it simple for now.
        with open(self.session_file, "a") as f:
            if not self.flushed:
                for e in self.entries:
                    f.write(e.model_dump_json(by_alias=True) + "\n")
                self.flushed = True
            else:
                f.write(entry.model_dump_json(by_alias=True) + "\n")

    def append_message(self, message: AgentMessage) -> str:
        entry = SessionMessageEntry(
            id=self._generate_id(),
            parent_id=self.leaf_id,
            timestamp=self._now_iso(),
            message=message,
        )
        self._append_entry(entry)
        return entry.id

    def build_session_context(self) -> List[AgentMessage]:
        # Simple linear context for now, ignore branching for a moment
        messages = []
        # In a real implementation, we'd walk from leaf to root.
        # But for nuu's initial version, linear is fine if we don't support branch command yet.
        for entry in self.entries:
            if isinstance(entry, SessionMessageEntry):
                messages.append(entry.message)
            elif isinstance(entry, CompactionEntry):
                # TODO: handle compaction summary message
                pass
        return messages

    @staticmethod
    def create(cwd: str, session_dir: Optional[str] = None) -> "SessionManager":
        if not session_dir:
            # Simplified session dir
            safe_cwd = cwd.replace("/", "-").replace("\\", "-").replace(":", "-")
            session_dir = os.path.expanduser(f"~/.nuu/sessions/{safe_cwd}")
        return SessionManager(cwd, session_dir)
