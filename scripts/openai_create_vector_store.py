#!/usr/bin/env python3
"""
Скрипт для создания OpenAI Vector Store и загрузки PDF библиотеки.

Использование:
    python scripts/openai_create_vector_store.py
    
После выполнения:
    1. Скопируйте Vector Store ID из вывода
    2. Добавьте в .env: OPENAI_VECTOR_STORE_ID=vs-...
    3. Добавьте в GitHub Secrets: OPENAI_VECTOR_STORE_ID
"""

import os
import sys
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv
import time

# Загрузка .env
load_dotenv()

def create_vector_store_with_files():
    """Создает Vector Store и загружает все PDF из data/"""
    
    print("=" * 60)
    print("🚀 СОЗДАНИЕ OPENAI VECTOR STORE")
    print("=" * 60)
    
    # Шаг 1: Проверка API key
    print("\n1️⃣ Проверка конфигурации...")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("   ❌ OPENAI_API_KEY не найден в .env")
        sys.exit(1)
    print(f"   ✅ API Key: {api_key[:8]}...")
    
    client = OpenAI(api_key=api_key)
    
    # Шаг 2: Поиск PDF файлов
    print("\n2️⃣ Поиск PDF файлов...")
    data_dir = Path(__file__).parent.parent / "data"
    pdf_files = list(data_dir.glob("*.pdf"))
    
    if not pdf_files:
        print("   ❌ PDF файлы не найдены в data/")
        sys.exit(1)
    
    print(f"   ✅ Найдено файлов: {len(pdf_files)}")
    for pdf in pdf_files:
        size_mb = pdf.stat().st_size / (1024 * 1024)
        print(f"      • {pdf.name} ({size_mb:.2f} MB)")
    
    total_size_mb = sum(pdf.stat().st_size for pdf in pdf_files) / (1024 * 1024)
    print(f"   📦 Общий размер: {total_size_mb:.2f} MB")
    
    # Шаг 3: Создание Vector Store
    print("\n3️⃣ Создание Vector Store...")
    try:
        vector_store = client.vector_stores.create(
            name="Natrium CrossFit Knowledge Base",
            expires_after={
                "anchor": "last_active_at",
                "days": 365  # Хранить год после последнего использования
            }
        )
        vector_store_id = vector_store.id
        print(f"   ✅ Vector Store создан: {vector_store_id}")
        print(f"   📅 Срок хранения: 365 дней после последней активности")
    except Exception as e:
        print(f"   ❌ Ошибка создания Vector Store: {e}")
        sys.exit(1)
    
    # Шаг 4: Загрузка файлов
    print(f"\n4️⃣ Загрузка {len(pdf_files)} файлов...")
    uploaded_files = []
    failed_files = []
    
    for i, pdf_path in enumerate(pdf_files, 1):
        try:
            print(f"\n   [{i}/{len(pdf_files)}] Загрузка: {pdf_path.name}")
            
            with open(pdf_path, "rb") as f:
                file_obj = client.files.create(
                    file=f,
                    purpose="assistants"
                )
            
            print(f"      ✅ Файл загружен: {file_obj.id}")
            uploaded_files.append(file_obj.id)
            
            # Добавление файла в Vector Store
            client.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=file_obj.id
            )
            print(f"      ✅ Добавлен в Vector Store")
            
            time.sleep(0.5)  # Небольшая задержка между загрузками
            
        except Exception as e:
            print(f"      ❌ Ошибка: {e}")
            failed_files.append(pdf_path.name)
    
    # Шаг 5: Ожидание завершения индексации
    print(f"\n5️⃣ Ожидание завершения индексации...")
    max_wait = 300  # 5 минут максимум
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        vs_status = client.vector_stores.retrieve(vector_store_id)
        
        file_counts = vs_status.file_counts
        total = file_counts.total
        completed = file_counts.completed
        in_progress = file_counts.in_progress
        failed = file_counts.failed
        
        print(f"   📊 Статус: {completed}/{total} завершено, "
              f"{in_progress} в процессе, {failed} ошибок")
        
        if vs_status.status == "completed":
            print(f"   ✅ Индексация завершена!")
            break
        
        if vs_status.status == "failed":
            print(f"   ❌ Индексация провалилась")
            break
        
        time.sleep(5)
    else:
        print(f"   ⚠️ Превышено время ожидания (5 минут)")
    
    # Итоги
    print("\n" + "=" * 60)
    print("📊 ИТОГИ")
    print("=" * 60)
    print(f"\n✅ Успешно загружено: {len(uploaded_files)} файлов")
    if failed_files:
        print(f"❌ Ошибки загрузки: {len(failed_files)} файлов")
        for fname in failed_files:
            print(f"   • {fname}")
    
    print(f"\n🔑 Vector Store ID: {vector_store_id}")
    print(f"📦 Размер библиотеки: {total_size_mb:.2f} MB")
    
    # Расчет стоимости
    storage_cost_per_day = total_size_mb / 1024 * 0.10  # $0.10 per GB/day
    storage_cost_per_month = storage_cost_per_day * 30
    
    print(f"\n💰 СТОИМОСТЬ ХРАНЕНИЯ:")
    print(f"   • В день: ${storage_cost_per_day:.4f}")
    print(f"   • В месяц: ${storage_cost_per_month:.4f}")
    print(f"   • Поиск: $0.00003 за 1000 токенов")
    
    # Инструкции
    print("\n" + "=" * 60)
    print("📝 СЛЕДУЮЩИЕ ШАГИ")
    print("=" * 60)
    print("\n1. Добавьте в .env:")
    print(f"   OPENAI_VECTOR_STORE_ID={vector_store_id}")
    
    print("\n2. Добавьте в GitHub Secrets:")
    print("   Settings → Secrets and variables → Actions")
    print(f"   Name: OPENAI_VECTOR_STORE_ID")
    print(f"   Value: {vector_store_id}")
    
    print("\n3. Обновите deploy.yml:")
    print("   - Добавьте OPENAI_VECTOR_STORE_ID в env и envs")
    print("   - Добавьте update_env_var для OPENAI_VECTOR_STORE_ID")
    
    print("\n4. Перезапустите бота:")
    print("   bash run.sh")
    
    print("\n5. Проверьте в логах:")
    print("   Должно появиться: 'Vector Store: vs-...'")
    
    print("\n" + "=" * 60)
    print("✅ ГОТОВО!")
    print("=" * 60)
    
    return vector_store_id

if __name__ == "__main__":
    try:
        vector_store_id = create_vector_store_with_files()
    except KeyboardInterrupt:
        print("\n\n⚠️ Прервано пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Неожиданная ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
