import os
import sys
from dotenv import load_dotenv
from openai import OpenAI

def main():
    print("--- SMOKE TEST START ---")

    # 1. Check imports
    print("[1/3] Checking dependencies...", end=" ")
    try:
        import openai
        import dotenv
        print("OK")
    except ImportError as e:
        print(f"FAILED: {e}")
        return

    # 2. Check Environment
    print("[2/3] Checking API Key...", end=" ")
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        print("FAILED")
        print("Error: OPENAI_API_KEY not found or is default in .env file.")
        print("Please edit .env and add your valid OpenAI API Key.")
        return
    print("OK (Key found)")

    # 3. API Call
    print("[3/3] Testing API connection...", end=" ")
    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Say 'Hello World'"}],
            max_tokens=5
        )
        content = response.choices[0].message.content
        print(f"OK\nResponse: {content}")
        print("\n--- SMOKE TEST PASSED ---")
    except Exception as e:
        print(f"FAILED\nError: {e}")

if __name__ == "__main__":
    main()
