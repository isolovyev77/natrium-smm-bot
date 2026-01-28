#!/usr/bin/env python3
"""Диагностический скрипт для проверки подключения к Yandex Agent"""

import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

print("="*70)
print("🔍 ДИАГНОСТИКА YANDEX AGENT API")
print("="*70)

# Проверка переменных окружения
print("\n1️⃣ Проверка переменных окружения:")
api_key = os.getenv("YANDEX_CLOUD_API_KEY")
folder_id = os.getenv("YANDEX_FOLDER_ID")
agent_id = os.getenv("YANDEX_AGENT_ID")

print(f"   YANDEX_CLOUD_API_KEY: {'✅ Установлен' if api_key else '❌ Не найден'}")
if api_key:
    print(f"      Префикс: {api_key[:8]}...")

print(f"   YANDEX_FOLDER_ID: {'✅ Установлен' if folder_id else '❌ Не найден'}")
if folder_id:
    print(f"      Значение: {folder_id}")

print(f"   YANDEX_AGENT_ID: {'✅ Установлен' if agent_id else '❌ Не найден'}")
if agent_id:
    print(f"      Значение: {agent_id}")

if not all([api_key, folder_id, agent_id]):
    print("\n❌ ОШИБКА: Не все переменные окружения установлены!")
    exit(1)

# Проверка подключения к API
print("\n2️⃣ Проверка подключения к API:")
try:
    client = OpenAI(
        api_key=api_key,
        base_url="https://rest-assistant.api.cloud.yandex.net/v1",
        project=folder_id,
    )
    print("   ✅ Клиент OpenAI создан успешно")
except Exception as e:
    print(f"   ❌ Ошибка создания клиента: {e}")
    exit(1)

# Тестовый запрос (генерация тем)
print("\n3️⃣ Тестовый запрос (генерация тем):")
try:
    print("   Отправка запроса...")
    response = client.responses.create(
        prompt={
            "id": agent_id,
            "variables": {
                "TECHNIQUE": "zero_shot",
                "USER_THEME": "",
                "POST_LENGTH": "500"
            }
        },
        input="Сгенерируй 10 тем для постов о фитнесе. Каждая тема не более 5 слов."
    )

    result = response.output_text
    print(f"   ✅ Ответ получен!")
    print(f"   Длина ответа: {len(result)} символов")
    print(f"\n   Первые 200 символов:")
    print(f"   {result[:200]}...")

    if len(result) < 50:
        print(f"\n   ⚠️ ВНИМАНИЕ: Ответ слишком короткий!")
        print(f"   Полный ответ: '{result}'")

except Exception as e:
    print(f"   ❌ Ошибка при запросе: {e}")
    import traceback
    traceback.print_exc()
    exit(1)

# Тестовый запрос (генерация поста)
print("\n4️⃣ Тестовый запрос (генерация поста):")
try:
    print("   Отправка запроса...")
    response = client.responses.create(
        prompt={
            "id": agent_id,
            "variables": {
                "TECHNIQUE": "zero_shot",
                "USER_THEME": "Сон и восстановление",
                "POST_LENGTH": "500"
            }
        },
        input="Сгенерируй ПОСТ на тему: Сон и восстановление. Длина 500 символов."
    )

    result = response.output_text
    print(f"   ✅ Ответ получен!")
    print(f"   Длина ответа: {len(result)} символов")
    print(f"\n   Первые 200 символов:")
    print(f"   {result[:200]}...")

except Exception as e:
    print(f"   ❌ Ошибка при запросе: {e}")
    import traceback
    traceback.print_exc()
    exit(1)

print("\n" + "="*70)
print("✅ ДИАГНОСТИКА ЗАВЕРШЕНА!")
print("="*70)
