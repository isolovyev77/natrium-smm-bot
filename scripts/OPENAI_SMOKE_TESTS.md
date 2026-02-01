# Smoke Tests для проверки OpenAI API

Перед интеграцией OpenAI в бота необходимо проверить доступность API и инструментов.

## Требования

```bash
pip install --upgrade openai
```

## Переменные окружения

```bash
export OPENAI_API_KEY="sk-..."  # Ваш API ключ
export OPENAI_MODEL="gpt-4o"    # Модель (после проверки Шага 1)
```

## Шаг 1: Проверка доступных моделей

Определяем, какие модели доступны в вашем аккаунте:

```bash
python scripts/openai_check_models.py
```

**Что проверяется:**
- Подключение к OpenAI API
- Список доступных GPT моделей
- Рекомендуемые модели для бота

**Ожидаемый результат:**
```
✅ Найдено X GPT моделей:

🚀 GPT-4 модели:
   • gpt-4o
   • gpt-4-turbo
   • gpt-4

💡 Рекомендации для бота:
   Используйте в .env: OPENAI_MODEL=gpt-4o
```

## Шаг 2: Проверка Responses API

Проверяем базовый вызов API без tools:

```bash
python scripts/openai_check_responses.py
```

**Что проверяется:**
- Вызов `client.chat.completions.create()`
- Получение ответа от модели
- Подсчет токенов

**Ожидаемый результат:**
```
✅ Ответ получен: 'ок'

📊 Использовано токенов:
   • Input: 15
   • Output: 2
   • Total: 17
```

## Шаг 3: Проверка Web Search

Проверяем наличие встроенного web search tool:

```bash
python scripts/openai_check_web_search.py
```

**Что проверяется:**
- Поддержка `tools=[{"type": "web_search"}]`
- Получение актуальной информации из интернета
- Наличие источников в ответе

**Возможные результаты:**

✅ **Web Search доступен:**
```
✅ Web Search tool поддерживается!
📄 Ответ содержит источники и актуальную информацию
```

⚠️ **Web Search недоступен:**
```
⚠️ Web Search tool недоступен
💡 Рассмотрите интеграцию Tavily API для поиска
```

## Шаг 4: Проверка File Search + Vector Store

Проверяем возможность поиска по PDF через Vector Store:

```bash
python scripts/openai_check_file_search.py
```

**Что проверяется:**
1. Загрузка PDF в OpenAI Files
2. Создание Vector Store
3. Прикрепление файла к Vector Store
4. Создание Assistant с file_search
5. Запрос с использованием загруженного документа

**Ожидаемый результат:**
```
1️⃣ Загружаю PDF в OpenAI...
   ✅ Файл загружен: file-abc123

2️⃣ Создаю Vector Store...
   ✅ Vector Store создан: vs-xyz789

3️⃣ Прикрепляю файл к Vector Store...
   ✅ Файл прикреплен

4️⃣ Создаю Assistant с file_search...
   ✅ Assistant создан: asst-def456

5️⃣ Отправляю запрос с использованием file_search...
   ✅ File Search работает!

💡 Сохраните для использования в боте:
   OPENAI_VECTOR_STORE_ID=vs-xyz789
```

## Интерпретация результатов

### ✅ Все тесты прошли успешно

Можно приступать к интеграции OpenAI в бота:

1. Добавьте в `.env`:
   ```env
   OPENAI_API_KEY=sk-...
   OPENAI_MODEL=gpt-4o  # или другая из Шага 1
   ```

2. Запустите ingestion script для загрузки всех PDF:
   ```bash
   python scripts/openai_ingestion.py
   ```

3. Добавьте полученный Vector Store ID в `.env`:
   ```env
   OPENAI_VECTOR_STORE_ID=vs-...
   ```

### ⚠️ Web Search недоступен

Если Web Search tool не поддерживается:

1. **Вариант A**: Использовать только File Search (без актуальных данных из интернета)
2. **Вариант B**: Интегрировать [Tavily API](https://tavily.com) для web search:
   ```bash
   pip install tavily-python
   ```

### ❌ File Search не работает

Проверьте:
- Достаточно ли прав у API ключа (Files, Assistants, Vector Stores)
- Доступны ли Beta API в вашем аккаунте
- Привязана ли карта для оплаты

## Следующий шаг

После успешного прохождения всех smoke-tests:

→ Запустите **ingestion script** для загрузки всех PDF из `data/` в production Vector Store:

```bash
python scripts/openai_ingestion.py
```

См. [scripts/README.md](./README.md) для деталей.
