#!/usr/bin/env python3
"""
Скрипт для проверки File Search через Vector Store.
Шаг 4 из smoke-tests.

Создает тестовый Vector Store с одним PDF и проверяет поиск.

Usage:
    export OPENAI_API_KEY="sk-..."
    export OPENAI_MODEL="gpt-4o"
    python scripts/openai_check_file_search.py
"""

import os
import sys
import time
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


def check_file_search():
    """Проверяет работу File Search с Vector Store"""
    
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    
    if not api_key:
        print("❌ OPENAI_API_KEY не найден!")
        return False
    
    # Выбираем тестовый PDF
    data_dir = project_root / "data"
    test_pdf = data_dir / "CFJ_English_Level1_TrainingGuide_compressed.pdf"
    
    if not test_pdf.exists():
        print(f"❌ Тестовый PDF не найден: {test_pdf}")
        print("Убедитесь, что в папке data/ есть PDF файлы")
        return False
    
    print(f"🔍 Тестирую File Search с моделью: {model}")
    print(f"📄 Используем тестовый файл: {test_pdf.name}")
    client = OpenAI(api_key=api_key)
    
    vector_store_id = None
    file_id = None
    
    try:
        # Шаг 1: Загрузка файла
        print("\n1️⃣ Загружаю PDF в OpenAI...")
        with open(test_pdf, "rb") as f:
            file = client.files.create(
                file=f,
                purpose="assistants"
            )
        file_id = file.id
        print(f"   ✅ Файл загружен: {file_id}")
        
        # Шаг 2: Создание Vector Store
        print("\n2️⃣ Создаю Vector Store...")
        vector_store = client.beta.vector_stores.create(
            name="natrium_test_store"
        )
        vector_store_id = vector_store.id
        print(f"   ✅ Vector Store создан: {vector_store_id}")
        
        # Шаг 3: Прикрепление файла к Vector Store
        print("\n3️⃣ Прикрепляю файл к Vector Store...")
        client.beta.vector_stores.files.create(
            vector_store_id=vector_store_id,
            file_id=file_id
        )
        print(f"   ✅ Файл прикреплен")
        
        # Ждем индексации
        print("\n⏳ Ожидаю индексацию файла (10 сек)...")
        time.sleep(10)
        
        # Шаг 4: Создание Assistant с file_search
        print("\n4️⃣ Создаю Assistant с file_search...")
        assistant = client.beta.assistants.create(
            name="Test Assistant",
            instructions="Ты эксперт по CrossFit. Отвечай на основе загруженных материалов.",
            model=model,
            tools=[{"type": "file_search"}],
            tool_resources={
                "file_search": {
                    "vector_store_ids": [vector_store_id]
                }
            }
        )
        assistant_id = assistant.id
        print(f"   ✅ Assistant создан: {assistant_id}")
        
        # Шаг 5: Создание Thread и запрос
        print("\n5️⃣ Отправляю запрос с использованием file_search...")
        thread = client.beta.threads.create()
        
        client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content="Кратко перечисли основные принципы CrossFit из документа"
        )
        
        run = client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=assistant_id
        )
        
        # Ждем выполнения
        print("   ⏳ Ожидаю ответ...")
        while run.status in ['queued', 'in_progress']:
            time.sleep(2)
            run = client.beta.threads.runs.retrieve(
                thread_id=thread.id,
                run_id=run.id
            )
        
        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread.id)
            result = messages.data[0].content[0].text.value
            
            print(f"\n📄 Ответ:")
            print("-" * 60)
            print(result)
            print("-" * 60)
            
            print("\n✅ File Search работает!")
            print(f"\n💡 Сохраните для использования в боте:")
            print(f"   OPENAI_VECTOR_STORE_ID={vector_store_id}")
            
            # Очистка
            print("\n🧹 Очищаю тестовые ресурсы...")
            try:
                client.beta.assistants.delete(assistant_id)
                client.beta.vector_stores.delete(vector_store_id)
                client.files.delete(file_id)
                print("   ✅ Ресурсы удалены")
            except:
                print("   ⚠️  Не удалось удалить некоторые ресурсы")
            
            return True
        else:
            print(f"❌ Ошибка выполнения: {run.status}")
            print(f"   {run.last_error if hasattr(run, 'last_error') else 'Unknown error'}")
            return False
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        
        # Попытка очистки
        if vector_store_id or file_id:
            print("\n🧹 Пытаюсь очистить созданные ресурсы...")
            try:
                if vector_store_id:
                    client.beta.vector_stores.delete(vector_store_id)
                if file_id:
                    client.files.delete(file_id)
            except:
                pass
        
        return False


if __name__ == "__main__":
    success = check_file_search()
    sys.exit(0 if success else 1)
