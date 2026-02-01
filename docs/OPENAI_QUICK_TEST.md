# OpenAI Integration - Quick Test Guide

## Предварительная проверка

### 1. Проверьте .env файл

```bash
cat .env
```

Убедитесь что есть:
```bash
OPENAI_API_KEY=sk-proj-...
OPENAI_THEMES_MODEL=gpt-4o-mini
OPENAI_POST_MODEL=gpt-5.2
```

### 2. Smoke test

```bash
python scripts/openai_check_models.py
```

Ожидаемый вывод:
```
✅ Найдено 86 GPT моделей
📋 Рекомендуемые модели:
   • gpt-4o
   • gpt-4o-mini
   • gpt-5.2
```

## Тестирование в Telegram

### 1. Запустите бота локально

```bash
python src/main.py
```

### 2. Откройте бота в Telegram

### 3. Тест: Yandex (по умолчанию)

1. `/start`
2. Выберите технику: **COV + COK**
3. Выберите фокус: **💪 Тренировки**
4. Выберите тему из списка
5. Выберите длину: **📏 700 символов**
6. ✅ Пост должен сгенерироваться через Yandex

### 4. Тест: Переключение на OpenAI

1. Нажмите **⚙️ Настройки**
2. Нажмите **🤖 Выбрать AI модель**
3. Нажмите **🤖 OpenAI**
4. Увидите: **✅ Провайдер изменён на OpenAI**
5. Проверьте статус: должно быть **🤖 AI Провайдер: 🤖 OpenAI**

### 5. Тест: Генерация через OpenAI

1. Нажмите **⬅️ Назад** (или закройте настройки)
2. Нажмите **🔄 Начать заново**
3. Выберите технику: **COV + COK**
4. Выберите фокус: **🧠 Питание**
5. Выберите тему из списка
6. Выберите длину: **📏 1000 символов**
7. ✅ Пост должен сгенерироваться через OpenAI

### 6. Проверьте логи

В терминале должны быть строки:

```
INFO:src.telegram_bot:👤 User 12345: Using OpenAI for themes
INFO:src.openai_bot:🔍 OpenAI Responses API call:
INFO:src.openai_bot:   Model: gpt-4o-mini
INFO:src.openai_bot:   Tools: None
INFO:src.openai_bot:✅ OpenAI response received: 450 chars, 320 tokens

INFO:src.telegram_bot:👤 User 12345: Using OpenAI for post
INFO:src.openai_bot:🔍 OpenAI Responses API call:
INFO:src.openai_bot:   Model: gpt-5.2
INFO:src.openai_bot:   Tools: None
INFO:src.openai_bot:✅ OpenAI response received: 1050 chars, 890 tokens
```

### 7. Тест: Статистика

1. Откройте **⚙️ Настройки**
2. Нажмите **📊 Показать статистику сессии**
3. Проверьте:
   - ✅ Количество запросов > 0
   - ✅ Токены: input/output
   - ✅ Стоимость рассчитана

### 8. Тест: Переключение обратно на Yandex

1. Откройте **⚙️ Настройки**
2. Нажмите **🤖 Выбрать AI модель**
3. Нажмите **🧠 Yandex**
4. Увидите: **✅ Провайдер изменён на Yandex**
5. Откройте **📊 Показать статистику сессии**
6. ✅ Счетчики должны быть сброшены (0 запросов)

### 9. Тест: Fallback (если OpenAI недоступен)

1. Временно отключите OpenAI:
   ```bash
   # В .env закомментируйте:
   # OPENAI_API_KEY=sk-proj-...
   ```
2. Перезапустите бота: `Ctrl+C` → `python src/main.py`
3. Логи покажут:
   ```
   ⚠️ OpenAI не доступен: OPENAI_API_KEY not found
   ```
4. Откройте **⚙️ Настройки** → **🤖 Выбрать AI модель**
5. Попробуйте выбрать **🤖 OpenAI**
6. ✅ Должно появиться: **❌ OpenAI недоступен. Проверьте OPENAI_API_KEY**

## Проверка успешности

### ✅ Все тесты пройдены если:

1. Yandex генерирует темы и посты
2. OpenAI генерирует темы и посты
3. Переключение между провайдерами работает
4. Статистика отображается корректно
5. Статистика сбрасывается при смене провайдера
6. Fallback срабатывает при недоступности OpenAI
7. В логах видны правильные роуты (`User X: Using OpenAI/Yandex`)

### ❌ Проблемы

**Бот не запускается:**
- Проверьте `TELEGRAM_BOT_TOKEN` в `.env`
- Проверьте что установлены зависимости: `pip install -r requirements.txt`

**OpenAI недоступен:**
- Проверьте `OPENAI_API_KEY` в `.env`
- Убедитесь что ключ валидный: `python scripts/openai_check_models.py`

**Генерация не работает:**
- Проверьте логи на ошибки
- Убедитесь что промпты загружаются: `prompts/agent_system_prompt.md`

**Темы не генерируются:**
- Yandex: проверьте `YANDEX_API_KEY` и `YANDEX_ASSISTANT_ID`
- OpenAI: проверьте логи API calls

## Следующие шаги

### Опционально: Vector Store

Если хотите добавить file_search:

1. Создайте Vector Store:
   ```bash
   python scripts/openai_ingestion.py
   ```

2. Добавьте ID в `.env`:
   ```bash
   OPENAI_VECTOR_STORE_ID=vs-abc123...
   ```

3. Перезапустите бота

4. В логах увидите:
   ```
   INFO:src.openai_bot:  Vector Store: vs-abc123...
   INFO:src.openai_bot:   Tools: ['file_search']
   ```

### Деплой на Production

```bash
# 1. Добавьте GitHub Secret
# Settings → Secrets → Actions → New repository secret
#   Name: OPENAI_API_KEY
#   Value: sk-proj-...

# 2. Push изменений
git push origin main

# 3. GitHub Actions автоматически задеплоит на VM
```

## Документация

- [OPENAI_INTEGRATION.md](OPENAI_INTEGRATION.md) - Полное руководство
- [OPENAI_MODELS.md](OPENAI_MODELS.md) - Сравнение моделей
