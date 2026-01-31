# Инструкция: Заполнение .env файла

## Проблема
Команда `/update_prompt` возвращает ошибку из-за отсутствия настроенных переменных окружения.

## Решение

### Шаг 1: Узнайте ваш Telegram ID
1. Откройте Telegram
2. Найдите бота [@userinfobot](https://t.me/userinfobot)
3. Отправьте ему любое сообщение (например, `/start`)
4. Бот ответит вашим ID, например: `Your user ID: 123456789`
5. Скопируйте это число

### Шаг 2: Заполните файл .env

Откройте файл [.env](.env) в корне проекта и заполните следующие переменные:

```bash
# 1. Yandex Cloud Folder ID
# Где взять: https://console.yandex.cloud/folders
# Выберите папку, скопируйте ID из адресной строки
YANDEX_FOLDER_ID=b1g...

# 2. Yandex Cloud API Key
# Где взять: https://console.yandex.cloud/iam/api-keys
# Нажмите "Создать ключ" -> "API-ключ"
YANDEX_CLOUD_API_KEY=AQVN...

# 3. Yandex Agent ID
# Где взять: https://console.yandex.cloud/ai-agents
# Откройте вашего агента, скопируйте ID из URL
YANDEX_AGENT_ID=faj...

# 4. Telegram Bot Token
# Где взять: @BotFather в Telegram
# Команда: /mybots -> выбрать бота -> API Token
TELEGRAM_BOT_TOKEN=1234567890:ABC...

# 5. Ваш Telegram ID (из Шага 1)
ADMIN_TELEGRAM_ID=123456789
```

### Шаг 3: Перезапустите бота

**Вариант А: Если бот запущен через systemd**
```bash
sudo systemctl restart natrium-smm-bot
sudo systemctl status natrium-smm-bot
```

**Вариант Б: Если бот запущен вручную**
1. Остановите текущий процесс (Ctrl+C)
2. Запустите снова:
```bash
python3 src/main.py
```

**Вариант В: GitHub Codespaces**
1. В настройках Codespace добавьте Secrets:
   - Settings -> Secrets and variables -> Codespaces
   - Добавьте каждую переменную отдельно
2. Перезапустите Codespace

### Шаг 4: Проверьте работу команды

В Telegram отправьте боту:
```
/update_prompt
```

Ожидаемый результат:
```
✅ Системный промпт успешно обновлён!

Агент Yandex Cloud теперь использует новые требования:
• Обязательный заголовок в CAPS
• Обязательный лид-затравка после заголовка
• Усиленный контроль структуры постов
```

---

## Частые проблемы

### ❌ "Эта команда доступна только администраторам"
**Причина:** ADMIN_TELEGRAM_ID не совпадает с вашим ID  
**Решение:** Проверьте ID через @userinfobot, обновите .env

### ❌ "Ошибка обновления промпта"
**Причина:** Неверные Yandex Cloud credentials  
**Решение:** Проверьте YANDEX_AGENT_ID, YANDEX_CLOUD_API_KEY, YANDEX_FOLDER_ID

### ❌ Бот не запускается после изменения .env
**Причина:** Синтаксическая ошибка в .env (пробелы, кавычки)  
**Решение:** Проверьте формат (без кавычек, без пробелов вокруг =)

---

## Проверка текущей конфигурации

Проверьте, что все переменные загружены:
```bash
cd /workspaces/natrium-smm-bot
python3 -c "import os; from dotenv import load_dotenv; load_dotenv(); print('ADMIN_ID:', os.getenv('ADMIN_TELEGRAM_ID')); print('AGENT_ID:', os.getenv('YANDEX_AGENT_ID')[:10] + '...' if os.getenv('YANDEX_AGENT_ID') else None)"
```

Если выводит `None` — файл .env не загружен или переменные не заполнены.
