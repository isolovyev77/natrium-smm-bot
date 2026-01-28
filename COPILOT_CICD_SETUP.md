# Инструкции для Copilot: Настройка CI/CD деплоя на Oracle VM

## 🎯 Цель

Настроить автоматический деплой проекта **natrium-smm-bot** из GitHub на Oracle VM сервер по схеме:
- Разработка в **GitHub Codespaces** (или локально)
- Коммит в **GitHub** (ветка `main`)
- Автоматический деплой на **Oracle VM** через GitHub Actions
- Автоматический перезапуск сервиса через systemd

---

## 📋 Предварительные требования

### На Oracle VM должно быть установлено:
- Ubuntu 20.04+ (или другой Linux)
- Python 3.11+
- Git
- systemd (для автозапуска)
- sudo права для пользователя

### Исходные данные для вашего проекта:
- IP адрес Oracle VM: `<YOUR_IP>` (получите у администратора)
- SSH пользователь на VM: `ubuntu` (или `opc`)
- Путь к проекту на VM: `/opt/natrium-smm-bot`
- Имя systemd сервиса: `natrium-smm-bot.service`

---

## 🔧 Часть 1: Настройка Oracle VM

### 1.1. Создание SSH Deploy Key на VM

Подключитесь к Oracle VM и выполните:

```bash
# Создаём SSH ключ специально для деплоя
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy_key -C "github-deploy-natrium-bot" -N ""

# Выводим ПРИВАТНЫЙ ключ (сохраните для GitHub Secrets)
cat ~/.ssh/github_deploy_key

# Выводим ПУБЛИЧНЫЙ ключ (добавьте в Deploy Keys репозитория)
cat ~/.ssh/github_deploy_key.pub
```

**⚠️ ВАЖНО: Сохраните оба ключа в безопасном месте!**

---

### 1.2. Настройка Git на VM для использования Deploy Key

```bash
cd /opt/natrium-smm-bot

# Убедитесь что используется SSH remote (не HTTPS)
git remote -v

# Если используется HTTPS, меняем на SSH
git remote set-url origin git@github.com:isolovyev77/natrium-smm-bot.git

# Настраиваем Git использовать deploy key
git config core.sshCommand "ssh -i ~/.ssh/github_deploy_key -o IdentitiesOnly=yes"

# Проверяем что pull работает
git pull origin main
```

**Проверка:**
```bash
# Должен успешно подтянуть изменения без запроса пароля
git pull origin main
```

---

### 1.3. Настройка sudoers (для перезапуска сервиса без пароля)

```bash
sudo visudo
```

Добавьте эти строки (замените `ubuntu` на вашего пользователя, если отличается):

```
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl stop natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl start natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl status natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/journalctl -u natrium-smm-bot *
```

Сохраните и выйдите (**Ctrl+X** → **Y** → **Enter**).

**Проверка:**

```bash
# Должно работать без запроса пароля
sudo systemctl status natrium-smm-bot
sudo systemctl restart natrium-smm-bot
```

---

### 1.4. Проверка systemd service файла

Убедитесь что сервис уже установлен и работает:

```bash
# Проверяем что файл существует
sudo cat /etc/systemd/system/natrium-smm-bot.service

# Проверяем статус
sudo systemctl status natrium-smm-bot
```

Если сервис не установлен, создайте его:

```bash
sudo cp /opt/natrium-smm-bot/natrium-smm-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable natrium-smm-bot
sudo systemctl start natrium-smm-bot
```

**Проверка логов:**

```bash
# Логи из systemd
sudo journalctl -u natrium-smm-bot -f

# Или логи из файлов (если настроены в service)
tail -f /var/log/natrium-smm-bot.log
tail -f /var/log/natrium-smm-bot-error.log
```

---

## 🐙 Часть 2: Настройка GitHub

### 2.1. Добавление Deploy Key (публичный ключ)

1. Откройте репозиторий на GitHub: https://github.com/isolovyev77/natrium-smm-bot
2. **Settings → Deploy keys → Add deploy key**
3. **Title:** `Oracle VM Deploy Key`
4. **Key:** вставьте содержимое `~/.ssh/github_deploy_key.pub` (публичный ключ)
5. ⚠️ **Allow write access:** **НЕ СТАВЬТЕ** галочку (только чтение!)
6. Нажмите **Add key**

---

### 2.2. Добавление GitHub Secrets

1. Откройте репозиторий на GitHub
2. **Settings → Secrets and variables → Actions → New repository secret**

Добавьте следующие секреты:

#### Обязательные секреты для SSH подключения:

| Secret Name | Описание | Пример значения |
|-------------|----------|----------------|
| **DEPLOY_KEY** | Приватный SSH ключ для деплоя | Содержимое `~/.ssh/github_deploy_key` (весь текст включая `-----BEGIN` и `-----END`) |
| **ORACLE_SSH_HOST** | IP адрес Oracle VM | `123.45.67.89` |
| **ORACLE_SSH_USER** | SSH пользователь на VM | `ubuntu` (или `opc`) |
| **ORACLE_SSH_PORT** | SSH порт (опционально) | `22` (по умолчанию) |

#### Секреты приложения (для автоматического обновления .env):

| Secret Name | Описание | Откуда взять |
|-------------|----------|--------------|
| **TELEGRAM_BOT_TOKEN** | Токен Telegram бота | @BotFather в Telegram |
| **YANDEX_FOLDER_ID** | ID папки Yandex Cloud | Консоль Yandex Cloud |
| **YANDEX_AGENT_ID** | ID агента Yandex AI | Консоль Yandex Cloud |
| **YANDEX_API_KEY** | API ключ Yandex Cloud | Консоль Yandex Cloud |

**⚠️ Важно:** При добавлении `DEPLOY_KEY` убедитесь что:
- Копируете **приватный** ключ (не публичный!)
- Включаете **все строки** от `-----BEGIN OPENSSH PRIVATE KEY-----` до `-----END OPENSSH PRIVATE KEY-----`

---

### 2.3. Обновление GitHub Actions Workflow

Уже создан улучшенный workflow в файле `.github/workflows/deploy-new.yml`.

**Чтобы активировать его:**

```bash
# В вашем workspace
cd /workspaces/natrium-smm-bot

# Удаляем старый workflow и переименовываем новый
rm .github/workflows/deploy.yml
mv .github/workflows/deploy-new.yml .github/workflows/deploy.yml

# Коммитим изменения
git add .github/workflows/deploy.yml
git commit -m "feat: улучшен CI/CD workflow с auto-update .env"
git push origin main
```

**Что изменилось в новом workflow:**

✅ Использует более надежный `appleboy/ssh-action@v1.0.3`  
✅ Автоматически обновляет `.env` файл из GitHub Secrets  
✅ Более чистый и читаемый код  
✅ Меньше шагов = быстрее деплой  

---

## ✅ Часть 3: Проверка работы деплоя

### 3.1. Первый тестовый деплой

1. Внесите любое изменение в код (например, в README):

```bash
cd /workspaces/natrium-smm-bot

# Делаем тестовое изменение
echo "\n<!-- Test auto-deploy -->" >> README.md

# Коммитим и пушим
git add README.md
git commit -m "test: проверка автодеплоя"
git push origin main
```

2. Откройте GitHub → **Actions**
3. Увидите запущенный workflow **"Deploy to Oracle Cloud"**
4. Кликните на него и смотрите логи в реальном времени

### 3.2. Что должно произойти:

```
📦 Pulling latest changes...
📝 Updating .env file...
  ✓ Updated TELEGRAM_BOT_TOKEN
  ✓ Updated YANDEX_FOLDER_ID
  ✓ Updated YANDEX_AGENT_ID
  ✓ Updated YANDEX_API_KEY
✅ .env updated
📚 Installing dependencies...
✅ Dependencies installed
🔄 Restarting service...
✅ Deployment completed!
● natrium-smm-bot.service - Natrium SMM Bot
   Loaded: loaded
   Active: active (running)
```

### 3.3. Проверка на сервере

```bash
# Подключитесь к Oracle VM
ssh ubuntu@<YOUR_ORACLE_IP>

# Проверьте что код обновился
cd /opt/natrium-smm-bot
git log -1

# Проверьте статус сервиса
sudo systemctl status natrium-smm-bot

# Проверьте логи
sudo journalctl -u natrium-smm-bot -n 50

# Или файловые логи
tail -f /var/log/natrium-smm-bot.log
```

---

## 🐛 Часть 4: Устранение проблем

### Проблема: "Host key verification failed"

**Причина:** SSH не может подключиться из-за проверки хоста.

**Решение:**
```bash
# На VM
ssh-keyscan github.com >> ~/.ssh/known_hosts
```

---

### Проблема: "Permission denied (publickey)"

**Причина:** GitHub Actions не может подключиться по SSH.

**Решение:**

1. Проверьте что Deploy Key (публичный) добавлен в **Settings → Deploy keys**
2. Проверьте что `DEPLOY_KEY` секрет содержит **приватный ключ**
3. Убедитесь что ключ скопирован **полностью** с headers

```bash
# На VM - проверьте формат приватного ключа
cat ~/.ssh/github_deploy_key
# Должен начинаться с -----BEGIN OPENSSH PRIVATE KEY-----
```

---

### Проблема: "sudo: a password is required"

**Причина:** sudoers не настроен правильно.

**Решение:**
```bash
sudo visudo

# Добавьте строки для вашего пользователя
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl * natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/journalctl -u natrium-smm-bot *
```

Проверка:
```bash
sudo systemctl status natrium-smm-bot  # Не должен запрашивать пароль
```

---

### Проблема: "git pull: Authentication failed"

**Причина:** Git не может использовать Deploy Key.

**Решение:**
```bash
cd /opt/natrium-smm-bot

# Проверяем remote URL
git remote -v
# Должен быть: git@github.com:isolovyev77/natrium-smm-bot.git

# Если HTTPS, меняем на SSH
git remote set-url origin git@github.com:isolovyev77/natrium-smm-bot.git

# Настраиваем использование ключа
git config core.sshCommand "ssh -i ~/.ssh/github_deploy_key -o IdentitiesOnly=yes"

# Тестируем
git pull origin main
```

---

### Проблема: Сервис не перезапускается

**Причина:** Ошибки в коде или зависимостях.

**Решение:**
```bash
# Смотрим детальные логи
sudo journalctl -u natrium-smm-bot -n 100 --no-pager

# Проверяем синтаксис service файла
sudo systemctl daemon-reload

# Пробуем запустить вручную
cd /opt/natrium-smm-bot
source venv/bin/activate
python src/telegram_bot.py  # Смотрим ошибки напрямую
```

---

### Проблема: ".env не обновляется из GitHub Secrets"

**Причина:** Секреты не добавлены в GitHub или неправильные имена.

**Решение:**

1. Проверьте что все секреты добавлены в **Settings → Secrets → Actions**
2. Имена должны точно совпадать (регистр важен):
   - `TELEGRAM_BOT_TOKEN`
   - `YANDEX_FOLDER_ID`
   - `YANDEX_AGENT_ID`
   - `YANDEX_API_KEY`

3. Проверьте на VM что .env обновляется:
```bash
cat /opt/natrium-smm-bot/.env
```

---

## 📚 Дополнительные ресурсы

### Документы этого проекта:
- [DEPLOYMENT.md](DEPLOYMENT.md) - детальная настройка деплоя
- [DEPLOY_QUICK.md](DEPLOY_QUICK.md) - быстрая настройка
- [natrium-smm-bot.service](natrium-smm-bot.service) - systemd сервис
- [.github/workflows/deploy.yml](.github/workflows/deploy.yml) - GitHub Actions

### Внешние ресурсы:
- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [appleboy/ssh-action](https://github.com/appleboy/ssh-action) - используемый SSH action
- [GitHub Deploy Keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)

---

## 🎬 Схема работы деплоя

```
┌─────────────────────────┐
│  GitHub Codespaces      │
│  или Local Development  │
└────────────┬────────────┘
             │ git push origin main
             ▼
┌─────────────────────────┐
│  GitHub Repository      │
│  (main branch)          │
└────────────┬────────────┘
             │ Trigger: on push
             ▼
┌─────────────────────────┐
│  GitHub Actions         │
│  (Ubuntu Runner)        │
└────────────┬────────────┘
             │ SSH Connection
             │ (with DEPLOY_KEY)
             ▼
┌─────────────────────────────────┐
│  Oracle VM                      │
│  (/opt/natrium-smm-bot)        │
│                                 │
│  1. git pull origin main        │
│  2. Update .env from secrets    │
│  3. pip install -r requirements │
│  4. sudo systemctl restart bot  │
└─────────────────────────────────┘
             │
             ▼
      🎉 Bot Running!
```

---

## 📝 Чек-лист для настройки CI/CD

### На Oracle VM:
- [ ] Создан SSH Deploy Key (`ssh-keygen -t ed25519`)
- [ ] Git настроен с SSH remote и Deploy Key
- [ ] Git config установлен: `core.sshCommand`
- [ ] sudoers настроен для команд systemctl без пароля
- [ ] systemd service создан и работает
- [ ] Проект клонирован в `/opt/natrium-smm-bot`
- [ ] Виртуальное окружение создано (`venv/`)
- [ ] .env файл создан с базовыми настройками

### На GitHub:
- [ ] Deploy Key (публичный) добавлен в **Settings → Deploy keys**
- [ ] Секрет `DEPLOY_KEY` (приватный ключ) добавлен
- [ ] Секреты `ORACLE_SSH_HOST`, `ORACLE_SSH_USER` добавлены
- [ ] Секреты приложения добавлены (TELEGRAM_BOT_TOKEN и др.)
- [ ] Workflow файл `.github/workflows/deploy.yml` обновлен

### Проверка:
- [ ] Тестовый коммит запустил деплой
- [ ] Workflow завершился успешно (зеленая галочка)
- [ ] Сервис перезапустился на VM
- [ ] Бот отвечает в Telegram
- [ ] Логи не содержат ошибок

---

## 🚀 Готово!

**Теперь каждый `git push` в ветку `main` будет автоматически:**
1. ✅ Подтягивать изменения на сервер
2. ✅ Обновлять .env из GitHub Secrets
3. ✅ Устанавливать новые зависимости
4. ✅ Перезапускать бота

**Наслаждайтесь автоматическим деплоем! 🎉**

---

## 💡 Полезные команды для разработки

### Просмотр логов деплоя в GitHub:
```
GitHub → Repository → Actions → Последний workflow
```

### Проверка статуса на сервере:
```bash
ssh ubuntu@<IP> "sudo systemctl status natrium-smm-bot"
ssh ubuntu@<IP> "sudo journalctl -u natrium-smm-bot -f"
```

### Ручной деплой без коммита:
```
GitHub → Actions → Deploy to Oracle Cloud → Run workflow
```

### Откат к предыдущей версии:
```bash
ssh ubuntu@<IP>
cd /opt/natrium-smm-bot
git log --oneline -5  # Смотрим последние коммиты
git reset --hard <commit-hash>
sudo systemctl restart natrium-smm-bot
```
