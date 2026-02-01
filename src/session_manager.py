import os
import json
import uuid
import glob
from datetime import datetime

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'sessions')

class SessionManager:
    def __init__(self):
        os.makedirs(SESSIONS_DIR, exist_ok=True)

    def create_session(self, model_config):
        session_id = str(uuid.uuid4())
        session_dir = os.path.join(SESSIONS_DIR, session_id)
        os.makedirs(os.path.join(session_dir, 'turns'), exist_ok=True)

        state = {
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
            "config": model_config,
            "summary": "",
            "turn_count": 0
        }

        self._save_state(session_id, state)
        return session_id, state

    def load_session(self, session_id):
        session_dir = os.path.join(SESSIONS_DIR, session_id)
        if not os.path.exists(session_dir):
            return None, []

        # Load state
        state_path = os.path.join(session_dir, 'state.json')
        with open(state_path, 'r') as f:
            state = json.load(f)

        # Load turns
        turns = []
        turns_pattern = os.path.join(session_dir, 'turns', '*.json')
        for turn_file in sorted(glob.glob(turns_pattern)):
            with open(turn_file, 'r') as f:
                turns.append(json.load(f))

        return state, turns

    def save_turn(self, session_id, turn_data):
        session_dir = os.path.join(SESSIONS_DIR, session_id)

        # Update state turn count
        state_path = os.path.join(session_dir, 'state.json')
        with open(state_path, 'r') as f:
            state = json.load(f)

        state['turn_count'] += 1
        state['last_updated'] = datetime.now().isoformat()

        # Save turn file
        turn_filename = f"{state['turn_count']:05d}.json"
        turn_path = os.path.join(session_dir, 'turns', turn_filename)
        with open(turn_path, 'w') as f:
            json.dump(turn_data, f, indent=2)

        # Save state (atomic-ish)
        self._save_state(session_id, state)

    def update_context(self, session_id, summary, buffer):
        session_dir = os.path.join(SESSIONS_DIR, session_id)
        state_path = os.path.join(session_dir, 'state.json')

        with open(state_path, 'r') as f:
            state = json.load(f)

        state['summary'] = summary
        state['recent_messages'] = buffer
        state['last_updated'] = datetime.now().isoformat()

        self._save_state(session_id, state)

    def _save_state(self, session_id, state):
        session_dir = os.path.join(SESSIONS_DIR, session_id)
        temp_path = os.path.join(session_dir, 'state.tmp')
        final_path = os.path.join(session_dir, 'state.json')

        with open(temp_path, 'w') as f:
            json.dump(state, f, indent=2)

        os.replace(temp_path, final_path)

    def list_sessions(self):
        sessions = []
        if not os.path.exists(SESSIONS_DIR):
            return []

        for d in os.listdir(SESSIONS_DIR):
            path = os.path.join(SESSIONS_DIR, d)
            if os.path.isdir(path):
                try:
                    with open(os.path.join(path, 'state.json'), 'r') as f:
                        state = json.load(f)
                        sessions.append(state)
                except Exception:
                    continue
        # Sort by last updated desc
        sessions.sort(key=lambda x: x.get('last_updated', x.get('created_at')), reverse=True)
        return sessions
