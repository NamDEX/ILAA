import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.json')

def load_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Config file not found at {CONFIG_PATH}. Using defaults.")
        return {
            "model": "gpt-4o",
            "temperature": 0.7,
            "max_output_tokens": 4096,
            "system_prompt": "You are a helpful AI assistant."
        }
