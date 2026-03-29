import unittest
import os
import shutil
import json
from src.session_manager import SessionManager
from src.config_loader import load_config

class TestSessionManager(unittest.TestCase):
    def setUp(self):
        # Use a temporary directory for sessions
        self.original_dir = os.path.dirname(os.path.abspath(__file__))
        # Mock SESSIONS_DIR by patching?
        # Easier to just use the actual one but clean up.
        self.sm = SessionManager()
        self.created_sessions = []

    def tearDown(self):
        # Clean up created sessions
        base_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'sessions')
        for sid in self.created_sessions:
            path = os.path.join(base_dir, sid)
            if os.path.exists(path):
                shutil.rmtree(path)

    def test_create_and_load_session(self):
        config = {"model": "test"}
        session_id, state = self.sm.create_session(config)
        self.created_sessions.append(session_id)

        self.assertIsNotNone(session_id)
        self.assertEqual(state['config']['model'], "test")

        loaded_state, turns = self.sm.load_session(session_id)
        self.assertEqual(loaded_state['session_id'], session_id)
        self.assertEqual(turns, [])

    def test_save_turn(self):
        config = {"model": "test"}
        session_id, _ = self.sm.create_session(config)
        self.created_sessions.append(session_id)

        turn_data = {
            "user_message": {"role": "user", "content": "hi"},
            "assistant_message": {"role": "assistant", "content": "hello"},
            "model": "test",
            "timestamp": "now"
        }

        self.sm.save_turn(session_id, turn_data)

        state, turns = self.sm.load_session(session_id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]['user_message']['content'], "hi")
        self.assertEqual(state['turn_count'], 1)

    def test_update_context(self):
        config = {"model": "test"}
        session_id, _ = self.sm.create_session(config)
        self.created_sessions.append(session_id)

        summary = "This is a summary."
        buffer = [{"role": "user", "content": "recent"}]

        self.sm.update_context(session_id, summary, buffer)

        state, _ = self.sm.load_session(session_id)
        self.assertEqual(state['summary'], summary)
        self.assertEqual(state['recent_messages'], buffer)

class TestConfig(unittest.TestCase):
    def test_load_defaults(self):
        # If config file exists, it loads it.
        # We assume config.json exists in this env.
        config = load_config()
        self.assertIn("model", config)
        self.assertIn("temperature", config)

if __name__ == '__main__':
    unittest.main()
