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
