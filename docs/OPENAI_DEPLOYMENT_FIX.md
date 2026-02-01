# CRITICAL FIX: OpenAI Provider Deployment

## Проблема найдена ✅

**OPENAI_API_KEY не передавался на production сервер при деплое!**

Когда GitHub Actions выполнял деплой, он:
- ✅ Обновлял TELEGRAM_BOT_TOKEN
- ✅ Обновлял YANDEX_CLOUD_API_KEY
- ❌ **НЕ обновлял OPENAI_API_KEY**

Поэтому на сервере:
```bash
# В .env на сервере
OPENAI_API_KEY=  # <- пустой!
```

И в коде:
```python
self.openai_bot = OpenAIBot()  # ❌ ValueError: OPENAI_API_KEY not found
```

## Исправление

**Commit:** `512c599` - fix: Add OPENAI_API_KEY to deployment workflow

### Что изменено в `.github/workflows/deploy.yml`:

1. Добавлен в `env`:
```yaml
OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

2. Добавлен в `envs`:
```yaml
envs: TELEGRAM_BOT_TOKEN,YANDEX_FOLDER_ID,YANDEX_AGENT_ID,YANDEX_CLOUD_API_KEY,OPENAI_API_KEY
```

3. Добавлен в `update_env_var`:
```bash
update_env_var "OPENAI_API_KEY" "$OPENAI_API_KEY"
```

## Что нужно сделать СЕЙЧАС

### 1. Добавьте GitHub Secret (если еще не добавили)

Перейдите на:
```
https://github.com/isolovyev77/natrium-smm-bot/settings/secrets/actions
```

Нажмите **New repository secret**:
- **Name:** `OPENAI_API_KEY`
- **Value:** `sk-proj-...` (ваш ключ OpenAI)

### 2. Запустите деплой

**Вариант А: Автоматический (рекомендуется)**

Изменения уже в `main`, GitHub Actions запустится автоматически.

Проверьте статус:
```
https://github.com/isolovyev77/natrium-smm-bot/actions
```

**Вариант Б: Ручной триггер**

Перейдите:
```
https://github.com/isolovyev77/natrium-smm-bot/actions/workflows/deploy.yml
```

Нажмите **Run workflow** → **Run workflow**

### 3. Проверьте что деплой успешен

В Actions logs должны быть строки:
```
✓ Updated TELEGRAM_BOT_TOKEN
✓ Updated YANDEX_FOLDER_ID
✓ Updated YANDEX_AGENT_ID
✓ Updated YANDEX_CLOUD_API_KEY
✓ Added OPENAI_API_KEY          <-- НОВОЕ!
✅ .env updated
```

### 4. Проверьте логи на сервере

SSH на сервер:
```bash
ssh user@your-server

# Проверьте что ключ теперь есть
cd /opt/natrium-smm-bot
grep OPENAI_API_KEY .env
# Должно показать: OPENAI_API_KEY=sk-proj-...

# Проверьте логи бота
sudo journalctl -u natrium-smm-bot -f
```

Должно быть:
```
✅ OpenAIBot инициализирован
  Themes: gpt-4o-mini
  Posts: gpt-5.2
  Tools: []
```

### 5. Проверьте в Telegram

1. Откройте бота
2. **⚙️ Настройки** → **🤖 Выбрать AI модель**
3. Нажмите **🤖 OpenAI**
4. Должно появиться: **✅ Провайдер изменён на OpenAI**

Если видите:
```
❌ OpenAI недоступен
Проверьте OPENAI_API_KEY в .env
```

Значит деплой еще не отработал или секрет не добавлен.

## Проверка правильности

После деплоя выполните на сервере:

```bash
# 1. Проверка .env
cd /opt/natrium-smm-bot
cat .env | grep OPENAI_API_KEY

# 2. Проверка что бот видит ключ
venv/bin/python3 << 'EOF'
import os
from dotenv import load_dotenv
load_dotenv()
key = os.getenv("OPENAI_API_KEY")
if key:
    print(f"✅ OPENAI_API_KEY найден: {key[:20]}...")
else:
    print("❌ OPENAI_API_KEY не найден!")
EOF

# 3. Проверка OpenAI бота
venv/bin/python3 scripts/check_providers.py
```

Ожидаемый вывод:
```
🤖 OPENAI GPT
==================================================
✅ OPENAI_API_KEY: sk-proj-...
✅ OPENAI_THEMES_MODEL: gpt-4o-mini
✅ OPENAI_POST_MODEL: gpt-5.2
ℹ️  OPENAI_VECTOR_STORE_ID: не настроен (опционально)
✅ OpenAIBot инициализирован
   Themes Model: gpt-4o-mini
   Post Model: gpt-5.2
   Available Tools: None

📊 ИТОГ
==================================================
✅ Yandex: ДОСТУПЕН
✅ OpenAI: ДОСТУПЕН
```

## Timeline

1. **До исправления:**
   ```
   User → Нажал OpenAI
   Bot → ❌ OpenAI недоступен (нет API key)
   ```

2. **После commit `512c599`:**
   ```
   User → Push to main
   GitHub Actions → Деплой с OPENAI_API_KEY
   Server → .env обновлен
   Bot → ✅ OpenAIBot инициализирован
   User → Нажал OpenAI → ✅ Работает!
   ```

## Важно

GitHub Secret `OPENAI_API_KEY` должен быть добавлен **ДО** запуска деплоя.

Если секрет не добавлен:
- Workflow выполнится успешно
- Но `OPENAI_API_KEY` в .env будет пустой
- OpenAI останется недоступным

## Следующие деплои

Теперь при каждом `git push origin main`:
1. GitHub Actions автоматически запустится
2. Скачает последний код на сервер
3. **Обновит OPENAI_API_KEY из секрета**
4. Перезапустит бота
5. OpenAI будет доступен

Проблема решена ✅
