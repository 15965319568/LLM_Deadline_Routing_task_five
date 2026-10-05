"""Incremental SSE framing for the completions path.

HTTP headers, comments, empty deltas and [DONE] are not generated tokens.
"""
import codecs
import json


class FirstToken:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.buffer = ""
        self.found = False

    def feed(self, chunk):
        if self.found:
            return False
        self.buffer += self.decoder.decode(chunk)
        self.buffer = self.buffer.replace("\r\n", "\n")
        while "\n\n" in self.buffer:
            block, self.buffer = self.buffer.split("\n\n", 1)
            text = "\n".join(line[5:].lstrip(" ") for line in block.split("\n") if line.startswith("data:"))
            if not text or text == "[DONE]":
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if any(isinstance(choice.get("text"), str) and choice["text"] for choice in value.get("choices", [])):
                self.found = True
                return True
        return False
