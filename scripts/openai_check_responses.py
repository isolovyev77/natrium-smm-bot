#!/usr/bin/env python3
"""
Скрипт для проверки базового вызова Responses API.
Шаг 2 из smoke-tests.

Usage:
    export OPENAI_API_KEY="sk-..."
    export OPENAI_MODEL="gpt-4o"  # или другая модель из check_models
    python scripts/openai_check_responses.py
"""

import os
import sys
from pathlib import Path

# Добавляем корень проекта в PYTHONPATH
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Загружаем переменные из .env
from dotenv import load_dotenv
load_dotenv(project_root / ".env")

try:
    from openai import OpenAI
except ImportError:
    print("❌ OpenAI SDK не установлен!")
    print("Установите: pip install --upgrade openai")
    sys.exit(1)


def check_responses_api():
    """Проверяет базовый вызов Responses API"""
    
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    
    if not api_key:
        print("❌ OPENAI_API_KEY не найден!")
        return False
    
    print(f"🔍 Тестирую Responses API с моделью: {model}")
    client = OpenAI(api_key=api_key)
    
    try:
        print("📤 Отправляю тестовый запрос...")
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "Ответь одним словом: ок"}
            ],
            max_tokens=10
        )
        
        result = response.choices[0].message.content.strip()
        
        print(f"✅ Ответ получен: '{result}'")
        print(f"\n📊 Использовано токенов:")
        print(f"   • Input: {response.usage.prompt_tokens}")
        print(f"   • Output: {response.usage.completion_tokens}")
        print(f"   • Total: {response.usage.total_tokens}")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при вызове Responses API: {e}")
        print(f"\n💡 Возможные причины:")
        print(f"   • Модель '{model}' недоступна (проверьте через openai_check_models.py)")
        print(f"   • Недостаточно прав у API ключа")
        print(f"   • Проблемы с сетью")
        return False


if __name__ == "__main__":
    success = check_responses_api()
    sys.exit(0 if success else 1)
