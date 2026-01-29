# 🐛 Диагностика ошибок деплоя

## Частые проблемы и их решения

### 1. ❌ "Permission denied (publickey)"

**Причина:** Проблема с SSH ключами

**Проверьте:**
- [ ] Публичный Deploy Key добавлен в: https://github.com/isolovyev77/natrium-smm-bot/settings/keys
- [ ] Приватный DEPLOY_KEY добавлен в Secrets
- [ ] DEPLOY_KEY содержит **приватный** ключ (начинается с `-----BEGIN OPENSSH PRIVATE KEY-----`)
- [ ] Ключ скопирован полностью (от `-----BEGIN` до `-----END`)

**Решение:**
```bash
# Попросите Codex на VM выполнить:
cat ~/.ssh/github_deploy_natrium      # Приватный ключ
cat ~/.ssh/github_deploy_natrium.pub  # Публичный ключ

# Проверьте что они совпадают с добавленными в GitHub
```

---

### 2. ❌ "Host key verification failed"

**Причина:** GitHub не в known_hosts

**Решение:**
```bash
# Попросите Codex на VM выполнить:
ssh-keyscan github.com >> ~/.ssh/known_hosts
```

---

### 3. ❌ "Could not resolve hostname"

**Причина:** Неправильный ORACLE_SSH_HOST

**Проверьте:**
- [ ] ORACLE_SSH_HOST в Secrets содержит правильный IP адрес
- [ ] IP адрес без протокола (не `http://`, не `ssh://`)
- [ ] Только цифры и точки, например: `123.45.67.89`

**Получить правильный IP:**
```bash
# Попросите Codex на VM выполнить:
curl -s ifconfig.me
```

---

### 4. ❌ "sudo: a password is required"

**Причина:** sudoers не настроен

**Проверьте:**
```bash
# Попросите Codex на VM выполнить:
sudo -n systemctl status natrium-smm-bot
# Должно работать БЕЗ запроса пароля
```

**Решение:**
```bash
# Попросите Codex на VM выполнить:
cat << 'EOF' | sudo tee /etc/sudoers.d/natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl stop natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl start natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl status natrium-smm-bot
ubuntu ALL=(ALL) NOPASSWD: /bin/journalctl -u natrium-smm-bot *
EOF

sudo chmod 0440 /etc/sudoers.d/natrium-smm-bot
```

---

### 5. ❌ "Failed to restart natrium-smm-bot.service: Unit natrium-smm-bot.service not found"

**Причина:** Сервис не создан

**Решение:**
```bash
# Попросите Codex на VM выполнить:
sudo systemctl list-unit-files | grep natrium

# Если не найден, создайте сервис:
sudo cp /opt/natrium-smm-bot/natrium-smm-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable natrium-smm-bot
```

---

### 6. ❌ "git pull: fatal: could not read from remote repository"

**Причина:** Deploy Key не работает на VM

**Проверьте:**
```bash
# Попросите Codex на VM выполнить:
cd /opt/natrium-smm-bot
git config --get core.sshCommand
# Должно показать: ssh -i /home/ubuntu/.ssh/github_deploy_natrium...

# Тестируем подключение:
ssh -T -i ~/.ssh/github_deploy_natrium git@github.com
# Должно показать: Hi isolovyev77/natrium-smm-bot! You've successfully authenticated
```

**Решение:**
```bash
# Попросите Codex на VM выполнить:
cd /opt/natrium-smm-bot
git config core.sshCommand "ssh -i ~/.ssh/github_deploy_natrium -o IdentitiesOnly=yes"
git pull origin main  # Проверка
```

---

### 7. ❌ "Port 22: Connection refused"

**Причина:** Неправильный порт SSH

**Проверьте:**
- Если SSH работает на другом порту, добавьте `ORACLE_SSH_PORT` в Secrets
- Стандартный порт: 22

---

### 8. ❌ Деплой прошел, но бот не работает

**Проверьте логи на VM:**
```bash
# Попросите Codex на VM выполнить:
sudo systemctl status natrium-smm-bot
sudo journalctl -u natrium-smm-bot -n 100 --no-pager

# Проверьте .env
cat /opt/natrium-smm-bot/.env
# Должны быть реальные токены, не temp_will_be_updated
```

---

## 🔧 Быстрая проверка всех компонентов

Попросите Codex на VM выполнить этот скрипт проверки:

```bash
#!/bin/bash
echo "=== Проверка настройки CI/CD ==="
echo ""

echo "✓ Deploy Key существует?"
ls -l ~/.ssh/github_deploy_natrium* 2>/dev/null || echo "❌ Deploy Key не найден"

echo ""
echo "✓ Git настроен на Deploy Key?"
cd /opt/natrium-smm-bot
git config --get core.sshCommand || echo "❌ Git не настроен"

echo ""
echo "✓ Git pull работает?"
timeout 10 git pull origin main --dry-run 2>&1 | head -5

echo ""
echo "✓ Sudoers настроен?"
sudo -n systemctl status natrium-smm-bot >/dev/null 2>&1 && echo "✅ Sudoers OK" || echo "❌ Sudoers требует пароль"

echo ""
echo "✓ Сервис существует?"
sudo systemctl list-unit-files | grep natrium-smm-bot || echo "❌ Сервис не найден"

echo ""
echo "✓ Сервис запущен?"
sudo systemctl is-active natrium-smm-bot || echo "❌ Сервис не активен"

echo ""
echo "✓ .env файл существует?"
ls -l /opt/natrium-smm-bot/.env || echo "❌ .env не найден"

echo ""
echo "✓ Последние логи бота:"
sudo journalctl -u natrium-smm-bot -n 5 --no-pager 2>/dev/null || echo "❌ Нет логов"
```

---

## 📋 Чек-лист для отладки

### В GitHub Secrets должны быть:
- [ ] `DEPLOY_KEY` - приватный SSH ключ (многострочный, с BEGIN/END)
- [ ] `ORACLE_SSH_HOST` - IP адрес (например 123.45.67.89)
- [ ] `ORACLE_SSH_USER` - обычно `ubuntu`
- [ ] `TELEGRAM_BOT_TOKEN` - токен бота
- [ ] `YANDEX_FOLDER_ID` - ID папки
- [ ] `YANDEX_AGENT_ID` - ID агента  
- [ ] `YANDEX_API_KEY` - API ключ

### В GitHub Deploy Keys должен быть:
- [ ] Публичный ключ (одна строка, начинается с `ssh-ed25519`)
- [ ] ⚠️ **БЕЗ** галочки "Allow write access"

### На VM должно быть:
- [ ] Проект в `/opt/natrium-smm-bot`
- [ ] Deploy Key в `~/.ssh/github_deploy_natrium`
- [ ] Git настроен на использование Deploy Key
- [ ] Сервис `natrium-smm-bot.service` создан
- [ ] Sudoers настроен для systemctl без пароля
- [ ] .env файл существует (будет обновлен при деплое)

---

**Пришлите текст ошибки из GitHub Actions, и я помогу исправить!**
