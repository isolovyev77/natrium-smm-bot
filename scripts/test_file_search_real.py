#!/usr/bin/env python3
"""
Тест file_search с реальным Vector Store и вопросами про CrossFit.
"""

import os
import sys
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

def test_file_search():
    """Тестирует file_search с реальными вопросами"""
    
    print("=" * 60)
    print("🔍 ТЕСТ FILE_SEARCH С РЕАЛЬНЫМИ ДАННЫМИ")
    print("=" * 60)
    
    # Инициализация
    api_key = os.getenv("OPENAI_API_KEY")
    vector_store_id = os.getenv("OPENAI_VECTOR_STORE_ID")
    
    if not api_key or not vector_store_id:
        print("\n❌ Не найдены OPENAI_API_KEY или OPENAI_VECTOR_STORE_ID")
        sys.exit(1)
    
    print(f"\n✅ API Key: {api_key[:8]}...")
    print(f"✅ Vector Store: {vector_store_id}")
    
    client = OpenAI(api_key=api_key)
    
    # Тестовые вопросы
    test_questions = [
        "Какие основные методы развития выносливости описаны в материалах Богачева?",
        "Какие упражнения включены в гимнастический блок CrossFit согласно Level 1 Training Guide?",
        "Что такое авторегуляция тренировочной нагрузки и как её применять?"
    ]
    
    for i, question in enumerate(test_questions, 1):
        print(f"\n{'=' * 60}")
        print(f"ВОПРОС {i}: {question}")
        print("=" * 60)
        
        try:
            response = client.responses.create(
                model="gpt-4o-mini",  # Используем более дешевую модель для теста
                input=[
                    {
                        "role": "system",
                        "content": "Ты эксперт по CrossFit и спортивной подготовке. "
                                   "Отвечай кратко и по существу, используя информацию из документов."
                    },
                    {
                        "role": "user",
                        "content": question
                    }
                ],
                tools=[{
                    "type": "file_search",
                    "vector_store_ids": [vector_store_id]
                }],
                max_output_tokens=500
            )
            
            # Извлечение ответа
            # Responses API возвращает список контента напрямую
            if isinstance(response.output, list):
                # Ответ - это список объектов контента
                answer = "\n".join([
                    content.text if hasattr(content, 'text') else str(content)
                    for content in response.output
                ])
            else:
                # Ответ - это объект с полем content
                answer = response.output
            
            print(f"\n📝 ОТВЕТ:\n{answer}")
            
            # Статистика
            usage = response.usage
            print(f"\n📊 СТАТИСТИКА:")
            print(f"   Input tokens: {usage.input_tokens}")
            print(f"   Output tokens: {usage.output_tokens}")
            print(f"   Total tokens: {usage.total_tokens}")
            
        except Exception as e:
            print(f"\n❌ Ошибка: {e}")
    
    print(f"\n{'=' * 60}")
    print("✅ ТЕСТ ЗАВЕРШЕН")
    print("=" * 60)

if __name__ == "__main__":
    test_file_search()
