import os


DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
LOG_INPUT = os.environ.get("LOG_INPUT", "false").lower() == "true"
LOG_OUTPUT = os.environ.get("LOG_OUTPUT", "false").lower() == "true"
PORT = int(os.environ.get("PORT", "8000"))
