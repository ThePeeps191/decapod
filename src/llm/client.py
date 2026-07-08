from openai import OpenAI

import config
from llmresponse import LLMResponse

class Client:
    def __init__(self, API_KEY = config.API_KEY,
                 BASE_URL = config.BASE_URL,
                 FLASH_MODEL = config.FLASH_MODEL,
                 PRO_MODEL = config.PRO_MODEL,
                 SYSTEM_PROMPT = None,
                 MAX_MODEL = config.MAX_MODEL,
                 MYTHOS_MODEL = config.MYTHOS_MODEL,
                 MULTIMODAL_MODEL = config.MULTIMODAL_MODEL,
                 FREE_MODEL = config.FREE_MODEL):
        self.API_KEY = API_KEY
        self.BASE_URL = BASE_URL
        self.FLASH_MODEL = FLASH_MODEL
        self.PRO_MODEL = PRO_MODEL
        self.MAX_MODEL = MAX_MODEL
        self.MYTHOS_MODEL = MYTHOS_MODEL
        self.MULTIMODAL_MODEL = MULTIMODAL_MODEL
        self.FREE_MODEL = FREE_MODEL

        self.SYSTEM_PROMPT = SYSTEM_PROMPT

        self.latest_response = None
        self.chat_history = []

        if self.SYSTEM_PROMPT:
            self.chat_history.append({"role": "system", "content": self.SYSTEM_PROMPT})

        self.client = OpenAI(
            api_key = self.API_KEY,
            base_url = self.BASE_URL
        )

    def message(self, content: str, model = "FLASH", output = True) -> LLMResponse:
        self.chat_history.append({"role": "user", "content": content})

        model = model.upper()
        try:
            cur_model = getattr(self, f"{model}_MODEL")
        except AttributeError:
            cur_model = self.FLASH_MODEL

        stream = self.client.chat.completions.create(
            model = cur_model,
            messages = self.chat_history,
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

        self.latest_response = LLMResponse("".join(thinking_parts), "".join(answer_parts))
        self.chat_history.append({"role": "assistant", "content": self.latest_response.answer})
        return self.latest_response
    
    def reset_history(self):
        self.chat_history = [{"role": "system", "content": self.SYSTEM_PROMPT}] if self.SYSTEM_PROMPT else []
        self.latest_response = None

    def insert_system(self, content: str):
        self.chat_history.append({"role": "system", "content": content})
    
    def undo_sent_msg(self):
        if len(self.chat_history) == 0:
            return -1
        

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

    client.reset_history()
    msg3 = client.message("What was said in the last message?")

    client.reset_history()
    msg3 = client.message("What model are you?", model = "MAX") # expect GLM-5.2
