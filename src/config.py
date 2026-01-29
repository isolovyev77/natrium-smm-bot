import os
from dotenv import load_dotenv

# Загрузка переменных окружения из .env (если локально)
# В GitHub Codespaces/Actions секреты доступны автоматически
load_dotenv()

# Конфигурация YandexGPT
YANDEX_AGENT_ID = os.getenv('YANDEX_AGENT_ID')
YANDEX_API_KEY = os.getenv('YANDEX_CLOUD_API_KEY')
YANDEX_FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')

# Telegram Bot
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

if not YANDEX_AGENT_ID or not YANDEX_API_KEY:
    raise ValueError("YANDEX_AGENT_ID и YANDEX_CLOUD_API_KEY должны быть заданы в переменных окружения или .env файле")

# Пути к файлам
DATA_DIR = "data"
PROMPTS_DIR = "prompts"
OUTPUT_DIR = "output"