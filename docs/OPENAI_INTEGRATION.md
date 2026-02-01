# OpenAI Integration Guide

## Обзор

Бот поддерживает два AI провайдера:
- **Yandex YandexGPT** (по умолчанию)
- **OpenAI GPT-5.2** (опционально)

Пользователи могут переключаться между провайдерами через меню настроек.

## Архитектура

### OpenAI: 2-шаговый пайплайн

```
1. Генерация тем:
   gpt-4o-mini → быстро, дешево, разнообразие

2. Генерация поста:
   gpt-5.2 → качество текста, креативность
```

### Responses API

OpenAI бот использует **Responses API** (не Chat Completions):

```python
response = client.responses.create(
    model="gpt-5.2",
    input=full_prompt,
    tools=[{
        "type": "file_search",
        "vector_store_ids": [vector_store_id]
    }]
)
```

### Hosted Tools

- **file_search**: поиск по PDF библиотеке (опционально)
- **web_search**: веб-поиск (требует отдельной настройки)

## Конфигурация

### .env файл

```bash
# OpenAI API Key
OPENAI_API_KEY=sk-proj-...

# 2-step pipeline models
OPENAI_THEMES_MODEL=gpt-4o-mini
OPENAI_POST_MODEL=gpt-5.2

# Vector Store (опционально)
# OPENAI_VECTOR_STORE_ID=vs-...
```

### GitHub Secrets (Production)

Добавьте в Settings → Secrets and variables → Actions:
- `OPENAI_API_KEY`

## Использование

### Для пользователей

1. Откройте бота в Telegram
2. Нажмите **⚙️ Настройки**
3. Выберите **🤖 Выбрать AI модель**
4. Выберите провайдера:
   - 🧠 **Yandex** - стабильно, быстро
   - 🤖 **OpenAI** - высокое качество

### Для разработчиков

#### Добавление Vector Store

1. Создайте Vector Store:
   ```bash
   python scripts/openai_ingestion.py
   ```

2. Скопируйте ID из вывода:
   ```
   ✅ Vector Store создан: vs-abc123...
   ```

3. Добавьте в `.env`:
   ```bash
   OPENAI_VECTOR_STORE_ID=vs-abc123...
   ```

#### Роутинг провайдеров

`telegram_bot.py` автоматически роутит запросы:

```python
provider = get_user_ai_provider(user_id)

if provider == 'openai' and self.openai_bot:
    themes, usage = self.openai_bot.generate_themes(...)
else:
    themes, usage = self.natrium_bot.generate_themes(...)
```

## Стоимость

| Провайдер | Модель       | Цена за 1M токенов | ~Пост |
|-----------|--------------|---------------------|-------|
| Yandex    | YandexGPT    | ₽1200 / ₽1200      | ₽0.035 |
| OpenAI    | gpt-4o-mini  | $0.15 / $0.60      | $0.001 |
| OpenAI    | gpt-5.2      | $3.00 / $15.00     | $0.029 |

**Итого OpenAI**: ~$0.03 за пост (темы + пост)

## Fallback стратегия

При ошибках OpenAI бот автоматически использует Yandex:

```python
try:
    if provider == 'openai':
        return self.openai_bot.generate_post(...)
except Exception as e:
    logger.error(f"OpenAI failed: {e}")
    return self.natrium_bot.generate_post(...)
```

## Статистика

При смене провайдера счетчики токенов автоматически сбрасываются:

```python
set_user_ai_provider(user_id, 'openai')
# → USER_SESSION_STATS[user_id] сброшен
```

## Тестирование

### Smoke tests

```bash
# Проверка доступных моделей
python scripts/openai_check_models.py

# Базовый тест API
python scripts/openai_check_responses.py
```

### В Telegram

1. Запустите бота локально:
   ```bash
   python src/main.py
   ```

2. Откройте бота в Telegram

3. Проверьте:
   - ✅ Генерация тем (Yandex)
   - ✅ Генерация поста (Yandex)
   - ✅ Переключение на OpenAI
   - ✅ Генерация тем (OpenAI)
   - ✅ Генерация поста (OpenAI)
   - ✅ Статистика токенов

## Документация

- [OPENAI_MODELS.md](OPENAI_MODELS.md) - Сравнение моделей
- [OPENAI_SMOKE_TESTS.md](../scripts/OPENAI_SMOKE_TESTS.md) - Результаты тестов
- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- [OpenAI Cookbook: Responses](https://cookbook.openai.com/examples/responses_api/responses_example)

## FAQ

**Q: Почему Responses API, а не Chat Completions?**  
A: Hosted tools (file_search, web_search) доступны только через Responses API.

**Q: Нужен ли Vector Store для работы?**  
A: Нет, бот работает без него. Vector Store добавляет file_search функциональность.

**Q: Почему 2 модели для OpenAI?**  
A: gpt-4o-mini ($0.15/1M) дешево генерирует темы, gpt-5.2 ($3/1M) дает качественный текст поста.

**Q: Как добавить web_search?**  
A: В `openai_bot.py` добавьте в tools:
```python
tools.append({"type": "web_search"})
```

**Q: Что если OpenAI недоступен?**  
A: Бот автоматически использует Yandex. Пользователь получит уведомление при попытке переключения.
