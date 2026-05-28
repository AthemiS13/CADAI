import os
import json
import uuid
from datetime import datetime
from pathlib import Path

class SessionManager:
    def __init__(self, storage_dir=None):
        if storage_dir is None:
            self.storage_dir = Path.home() / ".cadai" / "sessions"
        else:
            self.storage_dir = Path(storage_dir)
            
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_session_path(self, session_id):
        return self.storage_dir / f"{session_id}.json"

    def create_session(self, title="New Session"):
        session_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        session_data = {
            "id": session_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "cached_code": ""
        }
        self.save_session(session_data)
        return session_data

    def save_session(self, session_data):
        session_data["updated_at"] = datetime.now().isoformat()
        with open(self._get_session_path(session_data["id"]), 'w', encoding='utf-8') as f:
            json.dump(session_data, f, indent=4)

    def get_session(self, session_id):
        path = self._get_session_path(session_id)
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None

    def get_all_sessions(self):
        sessions = []
        for file_path in self.storage_dir.glob("*.json"):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    sessions.append({
                        "id": data["id"],
                        "title": data.get("title", "Untitled"),
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at")
                    })
            except Exception:
                pass
        
        # Sort by updated_at descending
        sessions.sort(key=lambda x: x["updated_at"], reverse=True)
        return sessions

    def add_message(self, session_id, role, content):
        session = self.get_session(session_id)
        if session:
            session["messages"].append({
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat()
            })
            
            # If this is the first user message, maybe update the title
            if len(session["messages"]) == 1 and role == "user":
                # simplistic title generation: first 30 chars
                title = content[:30] + ("..." if len(content) > 30 else "")
                session["title"] = title

            self.save_session(session)
            return session
        return None

    def update_cached_code(self, session_id, code):
        session = self.get_session(session_id)
        if session:
            session["cached_code"] = code
            self.save_session(session)
            return True
        return False

    def delete_session(self, session_id):
        path = self._get_session_path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False
