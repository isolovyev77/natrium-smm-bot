#!/usr/bin/env python3
"""
Скрипт для загрузки всех PDF из data/ в OpenAI Vector Store.
Production ingestion script.

Этот скрипт нужно запустить ОДИН РАЗ перед первым использованием OpenAI в боте.
После запуска сохраните VECTOR_STORE_ID в .env файл.

Usage:
    export OPENAI_API_KEY="sk-..."
    python scripts/openai_ingestion.py
    
    # После успешной загрузки добавьте в .env:
    # OPENAI_VECTOR_STORE_ID=vs-xyz...
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


def ingest_pdfs():
    """Загружает все PDF из data/ в Vector Store"""
    
    api_key = os.getenv("OPENAI_API_KEY")
    
    if not api_key:
        print("❌ OPENAI_API_KEY не найден в переменных окружения!")
        print("Установите: export OPENAI_API_KEY='sk-...'")
        return None
    
    # Находим все PDF в data/
    data_dir = project_root / "data"
    pdf_files = list(data_dir.glob("*.pdf"))
    
    if not pdf_files:
        print(f"❌ PDF файлы не найдены в {data_dir}")
        return None
    
    print(f"📚 Найдено {len(pdf_files)} PDF файлов для загрузки")
    print(f"📁 Директория: {data_dir}\n")
    
    client = OpenAI(api_key=api_key)
    uploaded_files = []
    vector_store_id = None
    
    try:
        # Шаг 1: Загрузка всех PDF
        print("1️⃣ Загружаю PDF в OpenAI Files API...")
        print("-" * 60)
        
        for i, pdf_path in enumerate(pdf_files, 1):
            print(f"   [{i}/{len(pdf_files)}] {pdf_path.name}...", end=" ")
            
            try:
                with open(pdf_path, "rb") as f:
                    file = client.files.create(
                        file=f,
                        purpose="assistants"
                    )
                uploaded_files.append({
                    'file_id': file.id,
                    'filename': pdf_path.name,
                    'size_mb': pdf_path.stat().st_size / (1024 * 1024)
                })
                print(f"✅ {file.id}")
            except Exception as e:
                print(f"❌ Ошибка: {e}")
                # Продолжаем с остальными файлами
        
        if not uploaded_files:
            print("\n❌ Ни один файл не был загружен!")
            return None
        
        print(f"\n✅ Загружено {len(uploaded_files)} из {len(pdf_files)} файлов")
        
        total_size = sum(f['size_mb'] for f in uploaded_files)
        print(f"📊 Общий размер: {total_size:.2f} MB")
        
        # Шаг 2: Создание Vector Store
        print(f"\n2️⃣ Создаю Vector Store 'natrium_knowledge_base'...")
        
        vector_store = client.beta.vector_stores.create(
            name="natrium_knowledge_base",
            expires_after=None  # Не удалять автоматически
        )
        vector_store_id = vector_store.id
        print(f"   ✅ Vector Store создан: {vector_store_id}")
        
        # Шаг 3: Прикрепление файлов к Vector Store
        print(f"\n3️⃣ Прикрепляю файлы к Vector Store...")
        print("-" * 60)
        
        attached_count = 0
        for i, file_info in enumerate(uploaded_files, 1):
            print(f"   [{i}/{len(uploaded_files)}] {file_info['filename']}...", end=" ")
            
            try:
                client.beta.vector_stores.files.create(
                    vector_store_id=vector_store_id,
                    file_id=file_info['file_id']
                )
                attached_count += 1
                print("✅")
            except Exception as e:
                print(f"❌ {e}")
        
        print(f"\n✅ Прикреплено {attached_count} файлов")
        
        # Шаг 4: Ожидание индексации
        print(f"\n4️⃣ Ожидаю индексацию файлов...")
        print("   ⏳ Это может занять несколько минут...")
        
        max_wait = 300  # 5 минут
        start_time = time.time()
        
        while time.time() - start_time < max_wait:
            vs = client.beta.vector_stores.retrieve(vector_store_id)
            
            if vs.status == 'completed':
                print(f"   ✅ Индексация завершена!")
                print(f"   📊 Проиндексировано файлов: {vs.file_counts.completed}")
                break
            elif vs.status == 'failed':
                print(f"   ❌ Ошибка индексации!")
                return None
            else:
                elapsed = int(time.time() - start_time)
                print(f"   ⏳ Статус: {vs.status}, прошло {elapsed}с...", end="\r")
                time.sleep(10)
        else:
            print(f"\n   ⚠️  Таймаут ожидания индексации (5 минут)")
            print(f"   Vector Store создан, но проверьте статус вручную")
        
        # Итоговая информация
        print("\n" + "=" * 60)
        print("🎉 УСПЕШНО! Vector Store готов к использованию")
        print("=" * 60)
        print(f"\n📋 Информация о Vector Store:")
        print(f"   • ID: {vector_store_id}")
        print(f"   • Имя: natrium_knowledge_base")
        print(f"   • Файлов: {len(uploaded_files)}")
        print(f"   • Размер: {total_size:.2f} MB")
        
        print(f"\n💾 ВАЖНО! Сохраните этот ID:")
        print(f"   export OPENAI_VECTOR_STORE_ID={vector_store_id}")
        print(f"\n   Или добавьте в .env файл:")
        print(f"   OPENAI_VECTOR_STORE_ID={vector_store_id}")
        
        print(f"\n📄 Список загруженных файлов:")
        for f in uploaded_files:
            print(f"   • {f['filename']} ({f['size_mb']:.2f} MB) - {f['file_id']}")
        
        return vector_store_id
        
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        
        # Попытка очистки при ошибке
        if vector_store_id:
            print(f"\n⚠️  Удаляю созданный Vector Store...")
            try:
                client.beta.vector_stores.delete(vector_store_id)
                print("   ✅ Vector Store удален")
            except:
                print(f"   ⚠️  Не удалось удалить Vector Store {vector_store_id}")
                print("   Удалите вручную через OpenAI Dashboard")
        
        if uploaded_files:
            print(f"\n⚠️  Удаляю загруженные файлы...")
            for f in uploaded_files:
                try:
                    client.files.delete(f['file_id'])
                except:
                    pass
        
        return None


def verify_vector_store(vector_store_id: str):
    """Проверяет Vector Store после создания"""
    
    api_key = os.getenv("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key)
    
    try:
        print(f"\n🔍 Проверка Vector Store {vector_store_id}...")
        vs = client.beta.vector_stores.retrieve(vector_store_id)
        
        print(f"   • Статус: {vs.status}")
        print(f"   • Файлов: {vs.file_counts.completed}")
        
        if vs.status == 'completed' and vs.file_counts.completed > 0:
            print("   ✅ Vector Store готов к использованию!")
            return True
        else:
            print("   ⚠️  Vector Store не готов")
            return False
            
    except Exception as e:
        print(f"   ❌ Ошибка проверки: {e}")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("OpenAI Vector Store Ingestion Script")
    print("Загрузка базы знаний Natrium SMM Bot")
    print("=" * 60)
    print()
    
    # Проверяем существующий Vector Store ID
    existing_vs_id = os.getenv("OPENAI_VECTOR_STORE_ID")
    if existing_vs_id:
        print(f"⚠️  Найден существующий OPENAI_VECTOR_STORE_ID: {existing_vs_id}")
        response = input("Создать новый Vector Store? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("Отменено пользователем")
            if verify_vector_store(existing_vs_id):
                sys.exit(0)
            else:
                print("\n⚠️  Существующий Vector Store недоступен")
                response = input("Создать новый? (yes/no): ")
                if response.lower() not in ['yes', 'y']:
                    sys.exit(1)
    
    vector_store_id = ingest_pdfs()
    
    if vector_store_id:
        print("\n✅ Готово! Теперь можно запускать бота с OpenAI")
        sys.exit(0)
    else:
        print("\n❌ Ошибка создания Vector Store")
        sys.exit(1)
