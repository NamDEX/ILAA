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
