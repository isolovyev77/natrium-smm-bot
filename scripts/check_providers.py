#!/usr/bin/env python3
"""
Скрипт для диагностики состояния провайдеров AI
"""
import os
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.append(str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from src.bot import NatriumBot
from src.openai_bot import OpenAIBot

load_dotenv()

def check_yandex():
    """Проверка Yandex YandexGPT"""
    print("🧠 YANDEX YandexGPT")
    print("=" * 50)
    
    try:
        api_key = os.getenv("YANDEX_API_KEY")
        assistant_id = os.getenv("YANDEX_ASSISTANT_ID")
        
        if not api_key:
            print("❌ YANDEX_API_KEY не найден в .env")
            return False
        
        if not assistant_id:
            print("❌ YANDEX_ASSISTANT_ID не найден в .env")
            return False
        
        print(f"✅ YANDEX_API_KEY: {api_key[:20]}...")
        print(f"✅ YANDEX_ASSISTANT_ID: {assistant_id[:20]}...")
        
        # Пытаемся инициализировать бота
        bot = NatriumBot()
        print(f"✅ NatriumBot инициализирован")
        print(f"   Assistant ID: {bot.assistant_id}")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False


def check_openai():
    """Проверка OpenAI"""
    print("\n🤖 OPENAI GPT")
    print("=" * 50)
    
    try:
        api_key = os.getenv("OPENAI_API_KEY")
        
        if not api_key:
            print("❌ OPENAI_API_KEY не найден в .env")
            print("   Бот будет работать только с Yandex")
            return False
        
        print(f"✅ OPENAI_API_KEY: {api_key[:20]}...")
        
        themes_model = os.getenv("OPENAI_THEMES_MODEL", "gpt-4o-mini")
        post_model = os.getenv("OPENAI_POST_MODEL", "gpt-5.2")
        vector_store_id = os.getenv("OPENAI_VECTOR_STORE_ID")
        
        print(f"✅ OPENAI_THEMES_MODEL: {themes_model}")
        print(f"✅ OPENAI_POST_MODEL: {post_model}")
        
        if vector_store_id:
            print(f"✅ OPENAI_VECTOR_STORE_ID: {vector_store_id[:20]}...")
        else:
            print(f"ℹ️  OPENAI_VECTOR_STORE_ID: не настроен (опционально)")
        
        # Пытаемся инициализировать бота
        bot = OpenAIBot()
        print(f"✅ OpenAIBot инициализирован")
        print(f"   Themes Model: {bot.themes_model}")
        print(f"   Post Model: {bot.post_model}")
        print(f"   Available Tools: {bot.available_tools if bot.available_tools else 'None'}")
        
        return True
        
    except ValueError as e:
        print(f"❌ Ошибка конфигурации: {e}")
        return False
    except Exception as e:
        print(f"❌ Ошибка инициализации: {e}")
        return False


def main():
    print("\n" + "=" * 50)
    print("🔍 ДИАГНОСТИКА AI ПРОВАЙДЕРОВ")
    print("=" * 50 + "\n")
    
    yandex_ok = check_yandex()
    openai_ok = check_openai()
    
    print("\n" + "=" * 50)
    print("📊 ИТОГ")
    print("=" * 50)
    
    if yandex_ok:
        print("✅ Yandex: ДОСТУПЕН")
    else:
        print("❌ Yandex: НЕДОСТУПЕН")
    
    if openai_ok:
        print("✅ OpenAI: ДОСТУПЕН")
    else:
        print("⚠️  OpenAI: НЕДОСТУПЕН (опционально)")
    
    print("\n" + "=" * 50)
    
    if yandex_ok:
        print("✅ Бот готов к работе!")
        if openai_ok:
            print("   Пользователи могут выбирать между Yandex и OpenAI")
        else:
            print("   Пользователи могут использовать только Yandex")
    else:
        print("❌ Бот НЕ готов к работе!")
        print("   Необходимо настроить Yandex API")


if __name__ == "__main__":
    main()
