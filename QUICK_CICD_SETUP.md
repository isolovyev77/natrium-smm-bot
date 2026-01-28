# 🚀 Быстрая настройка CI/CD для natrium-smm-bot

## Что это?

Автоматический деплой вашего Telegram бота на Oracle VM при каждом `git push` в `main`.

```
Codespaces → git push → GitHub Actions → Oracle VM → Бот перезапускается
```

---

## ⚡ Быстрый старт (5 минут)

### 1️⃣ На Oracle VM выполните:

```bash
# Скачайте и запустите скрипт автоматической настройки
curl -sSL https://raw.githubusercontent.com/isolovyev77/natrium-smm-bot/main/scripts/setup_deploy_key.sh | bash
```

**Или вручную:**

```bash
cd /opt/natrium-smm-bot
wget https://raw.githubusercontent.com/isolovyev77/natrium-smm-bot/main/scripts/setup_deploy_key.sh
chmod +x setup_deploy_key.sh
./setup_deploy_key.sh
```

Скрипт автоматически:
- ✅ Создаст SSH Deploy Key
- ✅ Настроит Git для автоматического pull
- ✅ Настроит sudoers для перезапуска без пароля
- ✅ Проверит systemd сервис

---

### 2️⃣ Добавьте ключи в GitHub

После выполнения скрипта вы получите два ключа:

#### А) Публичный ключ → GitHub Deploy Keys

1. Откройте: https://github.com/isolovyev77/natrium-smm-bot/settings/keys
2. **Add deploy key**
3. **Title:** `Oracle VM Deploy Key`
4. **Key:** вставьте **публичный** ключ (из вывода скрипта)
5. ⚠️ **НЕ** ставьте галочку "Allow write access"
6. **Add key**

#### Б) Приватный ключ → GitHub Secrets

1. Откройте: https://github.com/isolovyev77/natrium-smm-bot/settings/secrets/actions
2. **New repository secret**
3. Добавьте секреты:

| Name | Value | Где взять |
|------|-------|-----------|
| `DEPLOY_KEY` | Приватный SSH ключ | Из вывода скрипта (весь текст) |
| `ORACLE_SSH_HOST` | IP адрес вашего VM | `curl ifconfig.me` |
| `ORACLE_SSH_USER` | Пользователь SSH | `whoami` (обычно `ubuntu`) |
| `TELEGRAM_BOT_TOKEN` | Токен бота | @BotFather |
| `YANDEX_FOLDER_ID` | Yandex Folder ID | Yandex Cloud Console |
| `YANDEX_AGENT_ID` | Yandex Agent ID | Yandex Cloud Console |
| `YANDEX_API_KEY` | Yandex API Key | Yandex Cloud Console |

---

### 3️⃣ Активируйте новый workflow

```bash
cd /workspaces/natrium-smm-bot

# Замените старый workflow на новый
rm .github/workflows/deploy.yml
mv .github/workflows/deploy-new.yml .github/workflows/deploy.yml

# Закоммитьте
git add .
git commit -m "feat: обновлен CI/CD с автоматическим обновлением .env"
git push origin main
```

---

### 4️⃣ Проверьте деплой

1. Откройте: https://github.com/isolovyev77/natrium-smm-bot/actions
2. Смотрите как выполняется workflow
3. Должны увидеть:
   ```
   📦 Pulling latest changes...
   📝 Updating .env file...
   ✅ .env updated
   📚 Installing dependencies...
   🔄 Restarting service...
   ✅ Deployment completed!
   ```

---

## 🎯 Что происходит при деплое?

При каждом `git push origin main` автоматически:

1. **GitHub Actions** подключается к Oracle VM по SSH
2. Выполняет `git pull origin main`
3. Обновляет `.env` файл из GitHub Secrets
4. Устанавливает зависимости: `pip install -r requirements.txt`
5. Перезапускает бота: `systemctl restart natrium-smm-bot`

---

## 📋 Проверка что всё работает

### На Oracle VM:

```bash
# Проверка статуса сервиса
sudo systemctl status natrium-smm-bot

# Проверка логов
sudo journalctl -u natrium-smm-bot -f

# Проверка что git pull работает без пароля
cd /opt/natrium-smm-bot
git pull origin main

# Проверка что systemctl работает без пароля
sudo systemctl restart natrium-smm-bot
```

### В GitHub:

- [ ] Actions → Deploy to Oracle Cloud → Зеленая галочка ✅
- [ ] Deploy Keys → Oracle VM Deploy Key добавлен
- [ ] Secrets → Все 7 секретов добавлены

### В Telegram:

- [ ] Бот отвечает на команды
- [ ] Бот генерирует посты

---

## 🐛 Проблемы?

### "Permission denied (publickey)"

**Решение:** Проверьте что:
- Публичный ключ добавлен в Deploy Keys
- Приватный ключ добавлен в Secrets как `DEPLOY_KEY`
- На VM выполнен скрипт `setup_deploy_key.sh`

```bash
# На VM проверьте:
cat ~/.ssh/github_deploy_key.pub  # Должен совпадать с Deploy Key
cd /opt/natrium-smm-bot
git pull origin main              # Должен работать без пароля
```

---

### "sudo: a password is required"

**Решение:** Перезапустите скрипт или добавьте вручную:

```bash
sudo visudo

# Добавьте:
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl * natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/journalctl -u natrium-smm-bot *
```

---

### Деплой проходит, но бот не работает

**Решение:** Проверьте логи:

```bash
# На VM
sudo journalctl -u natrium-smm-bot -n 100 --no-pager

# Или
tail -f /var/log/natrium-smm-bot-error.log
```

Возможные причины:
- Неправильные токены в GitHub Secrets
- Не установлены зависимости
- Ошибка в коде

---

## 📚 Подробная документация

Полное руководство с подробными объяснениями: [COPILOT_CICD_SETUP.md](COPILOT_CICD_SETUP.md)

---

## 🎉 Готово!

Теперь при каждом изменении кода просто делайте:

```bash
git add .
git commit -m "feat: добавил новую функцию"
git push origin main
```

И через 1-2 минуты бот автоматически обновится на сервере! 🚀

---

## 💡 Полезные команды

```bash
# Посмотреть логи деплоя
https://github.com/isolovyev77/natrium-smm-bot/actions

# Запустить деплой вручную (без коммита)
Actions → Deploy to Oracle Cloud → Run workflow

# Проверить статус на сервере
ssh ubuntu@<IP> "sudo systemctl status natrium-smm-bot"

# Посмотреть логи в реальном времени
ssh ubuntu@<IP> "sudo journalctl -u natrium-smm-bot -f"
```

---

**Создано с ❤️ для автоматизации деплоя**
