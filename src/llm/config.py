import os

from dotenv import load_dotenv

load_dotenv()

# HackClub offers free AI
BASE_URL = "https://ai.hackclub.com/proxy/v1"
API_KEY = os.getenv("HACKCLUB_AI_API_KEY")
FLASH_MODEL = "deepseek/deepseek-v4-flash"
PRO_MODEL = "deepseek/deepseek-v4-pro"
MAX_MODEL = ""
MYTHOS_MODEL = ""
MULTIMODAL_MODEL = ""

# Use DeepSeek API instead
# BASE_URL = "https://api.deepseek.com"
# API_KEY = os.getenv("DEEPSEEK_API_KEY")
