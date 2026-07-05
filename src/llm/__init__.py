from openai import OpenAI

import config

class Client:
    def __init__(self, API_KEY = config.API_KEY, BASE_URL = config.BASE_URL, FLASH_MODEL = config.FLASH_MODEL, PRO_MODEL = config.PRO_MODEL):
        self.API_KEY = API_KEY
        self.BASE_URL = BASE_URL
        self.FLASH_MODEL = FLASH_MODEL
        self.PRO_MODEl = PRO_MODEL

        self.client = OpenAI(
            api_key = self.API_KEY,
            base_url = self.BASE_URL
        )

    def message(self, content: str, model = "flash", output = True):
        stream = self.client.chat.completions.create(
            model = self.FLASH_MODEL if model == "flash" else self.PRO_MODEL,
            messages = [
                {"role": "user", "content": content}
            ],
            stream = True
        )

        printed_thinking = False
        printed_answer = False

        thinking_parts = []
        answer_parts = []

        for chunk in stream:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            thinking = (
                getattr(delta, "reasoning", None)
                or getattr(delta, "reasoning_content", None)
            )

            if thinking:
                thinking_parts.append(thinking)

                if not printed_thinking:
                    if output: print("THINKING:")
                    printed_thinking = True
                
                if output: print(thinking, end = "", flush = True)
            
            if delta.content:
                answer_parts.append(delta.content)

                if not printed_answer:
                    if output: print("\n\nANSWER:")
                    printed_answer = True
                
                if output: print(delta.content, end = "", flush = True)

        if output: print()

        return ["".join(thinking_parts), "".join(answer_parts)]

if __name__ == "__main__":
    client = Client()
    msg = client.message("Hello, please describe in one concise but thorough sentence who you are and what you can do. Think thoroughly before you respond.")
    print("---")
    print(msg[0])
    print("---")
    print(msg[1])
