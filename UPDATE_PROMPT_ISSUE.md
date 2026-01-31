# ⚠️ ВАЖНО: Почему команда /update_prompt не работает

## Диагностика проблемы

Команда `/update_prompt` **ЕСТЬ В КОДЕ** ([telegram_bot.py#L291](src/telegram_bot.py#L291)), но она **НЕ МОЖЕТ РАБОТАТЬ** на текущей инфраструктуре.

## Причина

### Как работает команда:
1. Читает [prompts/agent_system_prompt.md](prompts/agent_system_prompt.md) из файловой системы
2. Отправляет PATCH запрос к Yandex Cloud API: `https://rest-assistant.api.cloud.yandex.net/v1/agents/{agent_id}`
3. Обновляет системный промпт в облаке

### Почему не работает:
- **ADMIN_TELEGRAM_ID** отсутствует в .env на VM
- Переменная **НЕ ПЕРЕДАЁТСЯ** через GitHub Secrets в [deploy.yml](.github/workflows/deploy.yml#L48-L54)
- На VM в .env есть только: `TELEGRAM_BOT_TOKEN`, `YANDEX_FOLDER_ID`, `YANDEX_AGENT_ID`, `YANDEX_CLOUD_API_KEY`

Вот секция из deploy.yml (строки 48-54):
```yaml
update_env_var "TELEGRAM_BOT_TOKEN" "$TELEGRAM_BOT_TOKEN"
update_env_var "YANDEX_FOLDER_ID" "$YANDEX_FOLDER_ID"
update_env_var "YANDEX_AGENT_ID" "$YANDEX_AGENT_ID"
update_env_var "YANDEX_CLOUD_API_KEY" "$YANDEX_CLOUD_API_KEY"
```

❌ ADMIN_TELEGRAM_ID нигде не упоминается!

## Текущий рабочий способ ✅

Вы правильно делаете:
1. Копируете содержимое [prompts/agent_system_prompt.md](prompts/agent_system_prompt.md) из GitHub
2. Открываете Yandex Cloud консоль: https://console.yandex.cloud/ai-agents
3. Выбираете агента
4. Вставляете новый промпт в веб-интерфейс
5. Сохраняете

**Это на 100% безопасно и надёжно!**

## Два варианта решения

### Вариант 1: Добавить ADMIN_TELEGRAM_ID в деплой (рекомендуется)

#### Шаг 1: Добавить секрет в GitHub
1. Откройте: https://github.com/isolovyev77/natrium-smm-bot/settings/secrets/actions
2. New repository secret
3. Name: `ADMIN_TELEGRAM_ID`
4. Value: `<ваш_telegram_id>` (узнать через @userinfobot)

#### Шаг 2: Обновить deploy.yml
```yaml
# В секции env (строка 17)
env:
  TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
  YANDEX_FOLDER_ID: ${{ secrets.YANDEX_FOLDER_ID }}
  YANDEX_AGENT_ID: ${{ secrets.YANDEX_AGENT_ID }}
  YANDEX_CLOUD_API_KEY: ${{ secrets.YANDEX_CLOUD_API_KEY }}
  ADMIN_TELEGRAM_ID: ${{ secrets.ADMIN_TELEGRAM_ID }}  # ← ДОБАВИТЬ

# В секции envs (строка 26)
envs: TELEGRAM_BOT_TOKEN,YANDEX_FOLDER_ID,YANDEX_AGENT_ID,YANDEX_CLOUD_API_KEY,ADMIN_TELEGRAM_ID  # ← ДОБАВИТЬ

# В секции update_env_var (после строки 54)
update_env_var "ADMIN_TELEGRAM_ID" "$ADMIN_TELEGRAM_ID"  # ← ДОБАВИТЬ
```

#### Шаг 3: Закоммитить и задеплоить
```bash
git add .github/workflows/deploy.yml
git commit -m "feat: добавлена поддержка ADMIN_TELEGRAM_ID для /update_prompt"
git push origin main
```

После деплоя команда `/update_prompt` заработает!

### Вариант 2: Продолжать обновлять вручную (проще)

Оставить всё как есть:
- Копировать промпт из [agent_system_prompt.md](prompts/agent_system_prompt.md)
- Вставлять в Yandex Cloud консоль
- Команду `/update_prompt` игнорировать

**Преимущества:**
- Визуальный контроль изменений
- Не нужно настраивать деплой
- Безопасность (не хранить ADMIN_ID)

**Недостатки:**
- Ручная работа (но она быстрая)

## Рекомендация

Если вас устраивает ручное обновление через веб-интерфейс — **оставьте как есть**.  
Команда `/update_prompt` это "nice to have", но не критична.

Если хотите автоматизировать — следуйте Варианту 1.

## Что я сделал неправильно

❌ Создал файл [.env](.env) в Codespaces — **УЖЕ УДАЛЁН**  
✅ Обновил [.env.example](.env.example) — это безопасно  
✅ Создал документацию [ENV_SETUP_GUIDE.md](ENV_SETUP_GUIDE.md) — полезно для будущих настроек

**Файл .env на VM остался нетронутым!**  
Деплой через [deploy.yml](.github/workflows/deploy.yml#L48-L54) обновляет только 4 переменные из Secrets.

## Проверка текущего состояния VM

Чтобы убедиться что на VM всё в порядке, можно проверить:
```bash
ssh <user>@<vm_ip>
cat /opt/natrium-smm-bot/.env | grep -v "KEY\|TOKEN"  # Покажет переменные без секретов
```

Должно быть примерно так:
```
YANDEX_FOLDER_ID=b1g...
YANDEX_AGENT_ID=faj...
LOG_LEVEL=INFO
```

(TELEGRAM_BOT_TOKEN и YANDEX_CLOUD_API_KEY скрыты фильтром)
