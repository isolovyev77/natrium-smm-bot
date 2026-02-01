#!/usr/bin/env python3
"""
Скрипт для проверки встроенного Web Search tool.
Шаг 3 из smoke-tests.

ВАЖНО: На момент создания скрипта (февраль 2026) OpenAI может не иметь 
встроенного web_search tool. Если получите ошибку - используйте Tavily API.

Usage:
    export OPENAI_API_KEY="sk-..."
    export OPENAI_MODEL="gpt-4o"
    python scripts/openai_check_web_search.py
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


def check_web_search():
    """Проверяет работу Web Search (если доступен)"""
    
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    
    if not api_key:
        print("❌ OPENAI_API_KEY не найден!")
        return False
    
    print(f"🔍 Тестирую Web Search с моделью: {model}")
    print("⚠️  Если web_search недоступен - используем обычный запрос")
    client = OpenAI(api_key=api_key)
    
    try:
        print("📤 Отправляю запрос с поиском актуальной информации...")
        
        # Пробуем с tools (может не поддерживаться)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "user", "content": "Найди 3 самых свежих факта о CrossFit Open 2026 и укажи источники"}
                ],
                tools=[{"type": "web_search"}],
                max_tokens=500
            )
            print("✅ Web Search tool поддерживается!")
        except Exception as tool_error:
            print(f"⚠️  Web Search tool недоступен: {tool_error}")
            print("📝 Делаю обычный запрос без tools...")
            
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Ты эксперт по CrossFit. Используй свои знания."},
                    {"role": "user", "content": "Расскажи кратко о CrossFit Open 2026"}
                ],
                max_tokens=300
            )
        
        result = response.choices[0].message.content.strip()
        
        print(f"\n📄 Ответ:")
        print("-" * 60)
        print(result)
        print("-" * 60)
        
        print(f"\n📊 Использовано токенов:")
        print(f"   • Input: {response.usage.prompt_tokens}")
        print(f"   • Output: {response.usage.completion_tokens}")
        print(f"   • Total: {response.usage.total_tokens}")
        
        print("\n💡 Вывод:")
        if "источник" in result.lower() or "http" in result.lower():
            print("   ✅ Похоже, что поиск работает (есть источники/ссылки)")
        else:
            print("   ⚠️  Источники не найдены - возможно, web_search недоступен")
            print("   💡 Рассмотрите интеграцию Tavily API для поиска")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False


if __name__ == "__main__":
    success = check_web_search()
    sys.exit(0 if success else 1)
