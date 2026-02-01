#!/usr/bin/env python3
"""
Скрипт для проверки доступных моделей в OpenAI аккаунте.
Шаг 1 из smoke-tests.

Usage:
    export OPENAI_API_KEY="sk-..."
    python scripts/openai_check_models.py
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


def check_models():
    """Получает список доступных моделей"""
    
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("❌ OPENAI_API_KEY не найден в переменных окружения!")
        print("Установите: export OPENAI_API_KEY='sk-...'")
        return False
    
    print("🔍 Подключаюсь к OpenAI API...")
    client = OpenAI(api_key=api_key)
    
    try:
        print("📋 Получаю список моделей...")
        models = client.models.list()
        
        # Фильтруем только GPT модели
        gpt_models = [m for m in models.data if 'gpt' in m.id.lower()]
        
        print(f"\n✅ Найдено {len(gpt_models)} GPT моделей:\n")
        
        # Группируем по типам
        gpt4_models = [m for m in gpt_models if 'gpt-4' in m.id]
        gpt35_models = [m for m in gpt_models if 'gpt-3.5' in m.id]
        other_models = [m for m in gpt_models if m not in gpt4_models and m not in gpt35_models]
        
        if gpt4_models:
            print("🚀 GPT-4 модели:")
            for m in sorted(gpt4_models, key=lambda x: x.id):
                print(f"   • {m.id}")
        
        if gpt35_models:
            print("\n💬 GPT-3.5 модели:")
            for m in sorted(gpt35_models, key=lambda x: x.id):
                print(f"   • {m.id}")
        
        if other_models:
            print("\n📦 Другие GPT модели:")
            for m in sorted(other_models, key=lambda x: x.id):
                print(f"   • {m.id}")
        
        # Рекомендации
        print("\n💡 Рекомендации для бота:")
        recommended = []
        for model_id in ['gpt-4o', 'gpt-4-turbo', 'gpt-4', 'gpt-4o-mini']:
            if any(m.id == model_id for m in gpt_models):
                recommended.append(model_id)
        
        if recommended:
            print(f"   Доступные рекомендуемые: {', '.join(recommended)}")
            print(f"\n   Используйте в .env: OPENAI_MODEL={recommended[0]}")
        else:
            print("   ⚠️ Рекомендуемые модели не найдены. Используйте любую из списка выше.")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка при получении списка моделей: {e}")
        return False


if __name__ == "__main__":
    success = check_models()
    sys.exit(0 if success else 1)
