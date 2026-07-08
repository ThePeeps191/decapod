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
    msg1 = client.message("What is 5 + 5?")
    print("---")
    print(msg1.thinking)
    print("---")
    print(msg1.answer)

    msg2 = client.message("What is 3 times the previous answer?")
    print("---")
    print(msg2.thinking)
    print("---")
    print(msg2.answer)
