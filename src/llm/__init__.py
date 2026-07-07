from openai import OpenAI

import config
from llmresponse import LLMResponse
from client import Client

__all__ = [
    "config",
    "LLMResponse",
    "Client"
]


if __name__ == "__main__":
    client = Client()
    msg = client.message("Hello, please describe in one concise but thorough sentence who you are and what you can do. Think thoroughly before you respond.")
    print("---")
    print(msg.thinking)
    print("---")
    print(msg.answer)
