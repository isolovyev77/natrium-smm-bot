# Как проверить что используется OpenAI

## Короткий ответ

**ДА**, если вы видите:
```
✍️ Генерирую пост (gpt-5.2) на тему: ...
```

То бот **попытался** использовать OpenAI. Но чтобы быть **100% уверенным**, смотрите на:

## 1. Маркер в конце поста

В конце каждого поста теперь есть строка:
```
🤖 Сгенерировано: gpt-5.2
```
или
```
🤖 Сгенерировано: YandexGPT
```

Это **гарантия** что пост создан указанным провайдером.

## 2. Ошибки показывают провайдера

Если OpenAI упадет, вы увидите:
```
❌ Ошибка генерации поста

Провайдер: (gpt-5.2)
Ошибка: Rate limit exceeded
```

**НЕТ автоматического fallback на Yandex!**

Если OpenAI не работает → вы увидите ошибку → можете вручную переключиться на Yandex.

## 3. Логи на сервере (для администратора)

SSH на сервер и смотрите логи:

```bash
ssh user@your-server
sudo journalctl -u natrium-smm-bot -f
```

Ищите строки:

**Успешная генерация через OpenAI:**
```
👤 User 123456: Using OpenAI for post (theme: Восстановление...)
🔍 OpenAI Responses API call:
   Model: gpt-5.2
   Tools: None
✅ OpenAI response received: 1050 chars, 890 tokens
===== RAW POST FROM OPENAI (before processing) =====
Provider: OpenAI (gpt-5.2)
===== FINAL TEXT SENT TO TELEGRAM =====
Provider: OpenAI (gpt-5.2)
```

**Успешная генерация через Yandex:**
```
👤 User 123456: Using Yandex for post (theme: Восстановление...)
===== RAW POST FROM YANDEX (before processing) =====
Provider: Yandex (YandexGPT)
===== FINAL TEXT SENT TO TELEGRAM =====
Provider: Yandex (YandexGPT)
```

**Ошибка OpenAI:**
```
👤 User 123456: Using OpenAI for post
❌ Ошибка генерации поста: Rate limit exceeded
   Provider: openai, Model: gpt-5.2
```

## 4. Статистика токенов

Если включена статистика (⚙️ Настройки → Включить статистику):

**OpenAI:**
```
📊 Генерация поста
Модель: gpt-5.2

🔢 Токены текущего запроса:
   • Входные: 1250
   • Выходные: 890
   • Всего: 2140

💰 Стоимость запроса: ~$0.000134  ← ДОЛЛАРЫ!
```

**Yandex:**
```
📊 Генерация поста
Модель: YandexGPT

🔢 Токены текущего запроса:
   • Входные: 1250
   • Выходные: 890
   • Всего: 2140

💰 Стоимость запроса: ~0.0350 ₽  ← РУБЛИ!
```

## Гарантии

### ✅ Можете быть уверены что используется OpenAI если:

1. **Видите `(gpt-5.2)` в начале генерации** И
2. **Видите `🤖 Сгенерировано: gpt-5.2` в конце поста** И
3. **Стоимость показана в долларах ($)** (если статистика включена)

### ❌ Признаки что использовался Yandex:

1. Видите `(YandexGPT)` в начале
2. Видите `🤖 Сгенерировано: YandexGPT` в конце
3. Стоимость показана в рублях (₽)

### ⚠️ Признаки ошибки:

1. Видите `❌ Ошибка генерации поста`
2. В ошибке указан `Провайдер: (gpt-5.2)`
3. Текст ошибки (например, "Rate limit exceeded")

## Почему нет автоматического fallback?

Изначально планировался fallback: OpenAI → (ошибка) → Yandex автоматически.

**Не реализовано потому что:**
1. Пользователь должен знать какой провайдер используется
2. OpenAI и Yandex могут давать разное качество текста
3. Стоимость разная ($0.03 vs ₽0.035 за пост)
4. Лучше явно показать ошибку → пользователь сам решает переключиться

## Как добавить fallback (если нужен)

Если хотите автоматический fallback:

1. Откройте `src/telegram_bot.py`
2. Найдите `generate_post_callback`
3. Замените блок:

```python
if provider == 'openai' and self.openai_bot:
    try:
        post, usage = self.openai_bot.generate_post(...)
        actual_provider = "OpenAI"
    except Exception as e:
        logger.warning(f"⚠️ OpenAI failed, falling back to Yandex: {e}")
        post, usage = self.natrium_bot.generate_post(...)
        actual_provider = "Yandex (fallback)"
        model_name = "YandexGPT (fallback from gpt-5.2)"
else:
    post, usage = self.natrium_bot.generate_post(...)
    actual_provider = "Yandex"
```

**Но рекомендуется оставить как есть** - явные ошибки лучше молчаливого fallback.

## Команды для проверки

### В Telegram:
1. Откройте бота
2. **⚙️ Настройки** → **✅ Включить статистику**
3. Сгенерируйте пост
4. Проверьте:
   - Начало: `(gpt-5.2)` или `(YandexGPT)`
   - Конец: `🤖 Сгенерировано: ...`
   - Статистика: `$` или `₽`

### На сервере:
```bash
# Логи в реальном времени
sudo journalctl -u natrium-smm-bot -f | grep -E "(Using|Provider|FROM)"

# Последние ошибки
sudo journalctl -u natrium-smm-bot -n 100 | grep -i error

# Проверка что OpenAI инициализирован
sudo journalctl -u natrium-smm-bot | grep "OpenAIBot"
```

Ожидаемый вывод при старте:
```
✅ OpenAIBot инициализирован
  Themes: gpt-4o-mini
  Posts: gpt-5.2
  Tools: []
```

Или если ключа нет:
```
⚠️ OpenAI не доступен: OPENAI_API_KEY not found
```

## FAQ

**Q: Что если вижу `(gpt-5.2)` но потом ошибку?**  
A: OpenAI попытался сгенерировать, но упал. Переключитесь на Yandex вручную.

**Q: Как узнать что OpenAI вообще работает?**  
A: Сгенерируйте 1 пост и проверьте маркер в конце `🤖 Сгенерировано: gpt-5.2`.

**Q: Может ли бот тихо переключиться на Yandex?**  
A: **НЕТ**. Fallback не реализован. Если OpenAI упадет → увидите ошибку.

**Q: Почему маркер `🤖 Сгенерировано` в конце поста?**  
A: Это гарантия какой провайдер использовался. Можете удалить перед публикацией в канал.

**Q: Как убрать маркер?**  
A: Просто не копируйте последнюю строку при вставке в канал. Или отредактируйте код в `telegram_bot.py` (строка с `post_with_marker`).
