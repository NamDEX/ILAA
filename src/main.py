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
