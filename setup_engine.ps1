
# Chat Engine MVP - One-Click Setup Script
# Run this in PowerShell to create all necessary files.

# 1. Create Directories
New-Item -ItemType Directory -Force -Path "src"
New-Item -ItemType Directory -Force -Path "config"
New-Item -ItemType Directory -Force -Path "data\sessions"

# 2. Write Files

# requirements.txt
@'
openai>=1.0.0
python-dotenv>=1.0.0
tiktoken>=0.5.0
googlesearch-python>=1.2.0
beautifulsoup4>=4.12.0
requests>=2.31.0
pymupdf>=1.23.0
'@ | Set-Content -Path "requirements.txt" -Encoding UTF8

# config/config.json
@'
{
  "model": "gpt-4o",
  "temperature": 0.7,
  "top_p": 1.0,
  "reasoning_effort": "medium",
  "max_output_tokens": 4096,
  "context_window_size": 128000,
  "summary_threshold": 4000,
  "system_prompt": "You are a helpful AI assistant.",
  "enable_web_search": false,
  "enable_multimodal": true
}
'@ | Set-Content -Path "config/config.json" -Encoding UTF8

# .env (example)
@'
OPENAI_API_KEY=your_api_key_here
'@ | Set-Content -Path ".env" -Encoding UTF8

# src/__init__.py
@'
'@ | Set-Content -Path "src/__init__.py" -Encoding UTF8

# src/config_loader.py
@'
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
'@ | Set-Content -Path "src/config_loader.py" -Encoding UTF8

# src/context_manager.py
@'
import tiktoken

class ContextManager:
    def __init__(self, client, config, initial_summary="", initial_buffer=None):
        self.client = client
        self.config = config
        self.summary = initial_summary
        self.buffer = initial_buffer if initial_buffer else []

        try:
            self.encoder = tiktoken.encoding_for_model(config.get("model", "gpt-4o"))
        except KeyError:
            self.encoder = tiktoken.get_encoding("cl100k_base")

    def add_message(self, message):
        self.buffer.append(message)

    def needs_pruning(self):
        """Checks token usage."""
        threshold = self.config.get("summary_threshold", 4000)
        current_tokens = self._count_tokens(self.buffer) + self._count_tokens(self.summary)
        return current_tokens > threshold

    def propose_summary(self):
        """Generates a summary of the oldest half of the buffer."""
        if not self.buffer:
            return None, 0

        # Determine how many to prune (half of buffer)
        prune_count = len(self.buffer) // 2
        if prune_count == 0:
            return None, 0

        to_summarize = self.buffer[:prune_count]

        text_to_summarize = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in to_summarize])

        prompt = f"""
        Current Summary: {self.summary}

        New Lines:
        {text_to_summarize}

        Update the summary to include the new lines. Keep it concise but capture key details, decisions, and context.
        """

        try:
            response = self.client.chat.completions.create(
                model=self.config.get("model", "gpt-4o"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            return response.choices[0].message.content, prune_count
        except Exception as e:
            print(f"[Summary Generation Failed: {e}]")
            return None, 0

    def commit_summary(self, new_summary, prune_count):
        """Updates summary and prunes buffer."""
        self.summary = new_summary
        self.buffer = self.buffer[prune_count:]

    def get_messages(self):
        msgs = [{"role": "system", "content": self.config.get("system_prompt", "You are a helpful AI assistant.")}]
        if self.summary:
            msgs.append({"role": "system", "content": f"SUMMARY OF PAST CONVERSATION:\n{self.summary}"})
        msgs.extend(self.buffer)
        return msgs

    def _count_tokens(self, text_or_list):
        if isinstance(text_or_list, list):
            text = "".join([m["content"] for m in text_or_list])
        else:
            text = text_or_list
        return len(self.encoder.encode(text))
'@ | Set-Content -Path "src/context_manager.py" -Encoding UTF8

# src/file_ingestion.py
@'
import fitz # pymupdf
import os

def ingest_file(filepath):
    if not os.path.exists(filepath):
        return None, "File not found."

    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".pdf":
        return ingest_pdf(filepath)
    elif ext in [".txt", ".md", ".json", ".py", ".csv", ".html"]:
        return ingest_text(filepath)
    else:
        return None, f"Unsupported file type: {ext}"

def ingest_pdf(filepath):
    try:
        doc = fitz.open(filepath)
        text = ""
        pages_read = 0
        images_detected = 0
        tables_detected = 0

        for page in doc:
            pages_read += 1
            text += f"\n--- Page {pages_read} ---\n"
            text += page.get_text()

            # Count images
            images_detected += len(page.get_images())

            # Count tables (if supported)
            if hasattr(page, "find_tables"):
                tables = page.find_tables()
                tables_detected += len(tables.tables)
                # We could extract table content here

        report = {
            "filename": os.path.basename(filepath),
            "pages_read": pages_read,
            "images_detected": images_detected,
            "tables_detected": tables_detected,
            "status": "Complete"
        }

        return text, report
    except Exception as e:
        return None, f"PDF Error: {e}"

def ingest_text(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        report = {
            "filename": os.path.basename(filepath),
            "type": "text",
            "size_chars": len(content),
            "status": "Complete"
        }
        return content, report
    except Exception as e:
        return None, f"Read Error: {e}"
'@ | Set-Content -Path "src/file_ingestion.py" -Encoding UTF8

# src/session_manager.py
@'
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
'@ | Set-Content -Path "src/session_manager.py" -Encoding UTF8

# src/tools.py
@'
from googlesearch import search
import requests
from bs4 import BeautifulSoup
import json

class WebSearchTool:
    def execute(self, query):
        print(f"\n[Searching: {query}]")
        try:
            # search() returns a generator of URLs
            urls = list(search(query, num_results=3))

            results = []
            for url in urls:
                print(f"[Browsing: {url}]")
                content = self.browse(url)
                results.append(f"Source: {url}\nContent: {content[:1000]}...\n")

            return "\n".join(results)
        except Exception as e:
            return f"Search failed: {e}"

    def browse(self, url):
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            resp = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(resp.content, 'html.parser')

            # Remove script and style elements
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.extract()

            text = soup.get_text()

            # Clean text
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks if chunk)

            return text
        except Exception as e:
            return f"Error reading {url}: {e}"

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for live information when you don't know the answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."}
                },
                "required": ["query"]
            }
        }
    }
]
'@ | Set-Content -Path "src/tools.py" -Encoding UTF8

# src/main.py
@'
import os
import sys
from dotenv import load_dotenv
from openai import OpenAI

# Allow running as script or module
import json
try:
    from src.config_loader import load_config
    from src.session_manager import SessionManager
    from src.context_manager import ContextManager
    from src.tools import WebSearchTool, TOOL_DEFINITIONS
    from src.file_ingestion import ingest_file
except ImportError:
    from config_loader import load_config
    from session_manager import SessionManager
    from context_manager import ContextManager
    from tools import WebSearchTool, TOOL_DEFINITIONS
    from file_ingestion import ingest_file

def main():
    # 1. Setup
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        print("Error: OPENAI_API_KEY not found. Please run smoke test or check .env.")
        return

    config = load_config()
    client = OpenAI(api_key=api_key)
    session_manager = SessionManager()

    print(f"--- Portable Chat Engine ---")
    print(f"Model: {config.get('model')} | Temp: {config.get('temperature')} | Top_P: {config.get('top_p')}")
    print(f"Context Limit: {config.get('max_output_tokens')}")

    # 2. Session Management
    sessions = session_manager.list_sessions()
    if sessions:
        print("\nExisting Sessions:")
        for idx, s in enumerate(sessions[:5]):
            print(f"{idx+1}. {s['session_id']} (Turns: {s['turn_count']}, Last: {s.get('last_updated', 'N/A')})")
        print("N. New Session")

        choice = input("\nSelect (1-5, N): ").strip().lower()
    else:
        choice = 'n'

    if choice.isdigit() and 1 <= int(choice) <= len(sessions):
        session_id = sessions[int(choice)-1]['session_id']
        state, turns = session_manager.load_session(session_id)
        print(f"\nLoaded Session: {session_id}")

        initial_buffer = state.get('recent_messages')
        if initial_buffer is None:
            initial_buffer = []
            for t in turns:
                initial_buffer.append(t['user_message'])
                initial_buffer.append(t['assistant_message'])

        ctx = ContextManager(client, config, state.get('summary', ''), initial_buffer)

        if turns:
            print("... (History loaded) ...")
            if ctx.summary:
                print(f"[Summary]: {ctx.summary[:100]}...")
            last_turn = turns[-1]
            print(f"You: {last_turn['user_message']['content']}")
            print(f"AI: {last_turn['assistant_message']['content']}")

    else:
        session_id, state = session_manager.create_session(config)
        ctx = ContextManager(client, config)
        print(f"\nStarted New Session: {session_id}")

    print("Type 'exit' or 'quit' to stop.")
    print("Commands: /attach <path> to upload files.")
    print("------------------------------------------------------------")

    search_tool = WebSearchTool()

    # 3. Loop
    while True:
        try:
            user_input = input("\nYou: ").strip()
            if not user_input:
                continue

            if user_input.lower() in ["exit", "quit"]:
                print("Goodbye!")
                break

            # Handle commands
            if user_input.startswith("/attach ") or user_input.startswith("/upload "):
                path = user_input.split(" ", 1)[1].strip()
                path = path.strip('"\'')
                content, report = ingest_file(path)
                if content:
                    print(f"[File Ingested: {report}]")
                    # Inject as system/user context
                    ctx.add_message({"role": "system", "content": f"User uploaded file {report['filename']}:\n{content}"})
                    continue
                else:
                    print(f"[Error: {report}]")
                    continue

            # Add user message
            user_msg = {"role": "user", "content": user_input}
            ctx.add_message(user_msg)

            # Tool Execution Loop
            while True:
                print("AI: ", end="", flush=True)
                try:
                    messages = ctx.get_messages()
                    use_tools = config.get("enable_web_search", False)
                    kwargs = {"tools": TOOL_DEFINITIONS} if use_tools else {}

                    stream = client.chat.completions.create(
                        model=config.get("model", "gpt-4o"),
                        messages=messages,
                        temperature=config.get("temperature", 0.7),
                        max_tokens=config.get("max_output_tokens", 4096),
                        top_p=config.get("top_p", 1.0),
                        stream=True,
                        **kwargs
                    )

                    full_content = ""
                    tool_calls = []

                    try:
                        for chunk in stream:
                            delta = chunk.choices[0].delta

                            # Content
                            if delta.content:
                                print(delta.content, end="", flush=True)
                                full_content += delta.content

                            # Tool Calls
                            if delta.tool_calls:
                                for tc_chunk in delta.tool_calls:
                                    if tc_chunk.index >= len(tool_calls):
                                        tool_calls.append({"id": "", "function": {"name": "", "arguments": ""}})

                                    tc_data = tool_calls[tc_chunk.index]
                                    if tc_chunk.id: tc_data["id"] += tc_chunk.id
                                    if tc_chunk.function.name: tc_data["function"]["name"] += tc_chunk.function.name
                                    if tc_chunk.function.arguments: tc_data["function"]["arguments"] += tc_chunk.function.arguments

                    except KeyboardInterrupt:
                        print("\n[Generation Interrupted]")
                        full_content += " [Interrupted]"

                    print() # Newline

                    if tool_calls:
                        # Add assistant message with tool calls
                        assistant_msg = {
                            "role": "assistant",
                            "content": full_content if full_content else None,
                            "tool_calls": [
                                {"id": tc["id"], "type": "function", "function": tc["function"]} for tc in tool_calls
                            ]
                        }
                        ctx.add_message(assistant_msg)

                        # Execute tools
                        for tc in tool_calls:
                            fname = tc["function"]["name"]
                            args_str = tc["function"]["arguments"]
                            if fname == "web_search":
                                try:
                                    args = json.loads(args_str)
                                    result = search_tool.execute(args["query"])
                                except Exception as e:
                                    result = str(e)

                                ctx.add_message({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result
                                })
                        # Loop continues to get next response
                        continue

                    else:
                        # Final response
                        ai_msg = {"role": "assistant", "content": full_content}
                        ctx.add_message(ai_msg)

                        # Save Turn
                        turn_data = {
                            "user_message": user_msg,
                            "assistant_message": ai_msg,
                            "model": config.get("model"),
                            "timestamp": state.get("last_updated"),
                            "request_payload": {
                                "messages": messages,
                                "model": config.get("model"),
                                "parameters": kwargs
                            }
                        }
                        session_manager.save_turn(session_id, turn_data)

                        # Manage Context
                        if ctx.needs_pruning():
                            print("\n[Context Limit Reached. Generating Summary...]")
                            proposal, prune_count = ctx.propose_summary()
                            if proposal:
                                print(f"\n--- Summary Proposal ---\n{proposal}\n------------------------")
                                action = input("Press Enter to accept, or type 'edit' to modify: ").strip().lower()

                                final_summary = proposal
                                if action == 'edit':
                                    print("Enter new summary (one line):")
                                    final_summary = input("> ").strip()

                                ctx.commit_summary(final_summary, prune_count)
                                print("[Summary Updated]")
                                session_manager.update_context(session_id, ctx.summary, ctx.buffer)

                        break # Exit Tool Loop

                except Exception as e:
                    print(f"\n[Error calling API: {e}]")
                    break # Exit Tool Loop

        except KeyboardInterrupt:
            print("\nGoodbye!")
            break

if __name__ == "__main__":
    main()
'@ | Set-Content -Path "src/main.py" -Encoding UTF8

Write-Host "Portable Chat Engine Setup Complete!"
Write-Host "1. Edit .env with your API Key"
Write-Host "2. Create/Activate venv"
Write-Host "3. pip install -r requirements.txt"
Write-Host "4. python src/main.py"
