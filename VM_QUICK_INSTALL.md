# 🚀 Быстрая установка Production VM

## Для Codex CLI или ручной установки

### Вариант 1: Полная автоматическая установка (рекомендуется)

```bash
curl -sSL https://raw.githubusercontent.com/isolovyev77/natrium-smm-bot/main/scripts/vm_prod_setup_script.sh | bash
```

**Что делает:**
- ✅ Устанавливает все зависимости
- ✅ Клонирует проект
- ✅ Создает venv и устанавливает пакеты
- ✅ Настраивает systemd сервис
- ✅ Создает Deploy Key для CI/CD
- ✅ Настраивает Git и sudoers
- ✅ Запускает бота

**Время:** 5-10 минут

---

### Вариант 2: Только настройка Deploy Key (если проект уже установлен)

```bash
cd /opt/natrium-smm-bot
curl -sSL https://raw.githubusercontent.com/isolovyev77/natrium-smm-bot/main/scripts/setup_deploy_key.sh | bash
```

---

## После установки

### 1. Заполните .env файл

```bash
nano /opt/natrium-smm-bot/.env
```

Замените `your_*_here` на реальные значения.

### 2. Добавьте ключи в GitHub

**Публичный Deploy Key:**
```bash
cat ~/.ssh/github_deploy_natrium.pub
```
Добавьте в: https://github.com/isolovyev77/natrium-smm-bot/settings/keys

**Приватный Deploy Key (GitHub Secret):**
```bash
cat ~/.ssh/github_deploy_natrium
```
Добавьте как `DEPLOY_KEY` в: https://github.com/isolovyev77/natrium-smm-bot/settings/secrets/actions

**Другие секреты:**
- `ORACLE_SSH_HOST` = ваш IP (получить: `curl ifconfig.me`)
- `ORACLE_SSH_USER` = ваш пользователь (получить: `whoami`)
- `TELEGRAM_BOT_TOKEN` = токен от @BotFather
- `YANDEX_FOLDER_ID`, `YANDEX_AGENT_ID`, `YANDEX_API_KEY`

### 3. Проверьте бота

```bash
# Статус сервиса
sudo systemctl status natrium-smm-bot

# Логи в реальном времени
sudo journalctl -u natrium-smm-bot -f

# Файловые логи
tail -f /var/log/natrium-smm-bot.log
```

### 4. Протестируйте в Telegram

Найдите бота и отправьте `/start`

---

## Полная документация

- [VM_PRODUCTION_SETUP.md](../VM_PRODUCTION_SETUP.md) - Подробная инструкция
- [COPILOT_CICD_SETUP.md](../COPILOT_CICD_SETUP.md) - Настройка CI/CD
- [scripts/README.md](README.md) - Описание всех скриптов

---

## Troubleshooting

### Бот не запускается
```bash
sudo journalctl -u natrium-smm-bot -n 100 --no-pager
```

### Git pull не работает
```bash
cd /opt/natrium-smm-bot
git config --get core.sshCommand  # Должен показать путь к ключу
```

### Sudoers требует пароль
```bash
sudo visudo -c -f /etc/sudoers.d/natrium-smm-bot
sudo systemctl status natrium-smm-bot  # Должно работать без пароля
```

---

**Готово! Теперь каждый `git push` будет автоматически деплоить бота на VM! 🎉**
