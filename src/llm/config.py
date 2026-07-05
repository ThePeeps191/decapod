import os

from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://ai.hackclub.com/proxy/v1"
API_KEY = os.getenv("HACKCLUB_AI_API_KEY")
FLASH_MODEL = "deepseek/deepseek-v4-flash"
PRO_MODEL = "deepseek/deepseek-v4-pro"
