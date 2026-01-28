# Quick Deployment Guide

## 🚀 Быстрый старт деплоя на Oracle Cloud

### Шаг 1: Подготовка сервера (один раз)

```bash
# Подключаемся к Oracle Cloud серверу
ssh ubuntu@<YOUR_ORACLE_IP>

# Устанавливаем зависимости
sudo apt update && sudo apt install -y python3.11 python3.11-venv git

# Создаем директорию для бота
sudo mkdir -p /opt/natrium-smm-bot
sudo chown $USER:$USER /opt/natrium-smm-bot
cd /opt/natrium-smm-bot

# Клонируем репозиторий
git clone https://github.com/isolovyev77/natrium-smm-bot.git .

# Создаем venv и устанавливаем зависимости
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Создаем .env с токенами (замените на свои значения)
cat > .env << 'EOF'
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
YANDEX_FOLDER_ID=your_yandex_folder_id
YANDEX_AGENT_ID=your_yandex_agent_id
YANDEX_API_KEY=your_yandex_api_key
EOF
chmod 600 .env

# Создаем systemd service
sudo tee /etc/systemd/system/natrium-smm-bot.service > /dev/null << 'EOF'
[Unit]
Description=Natrium SMM Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/natrium-smm-bot
Environment="PATH=/opt/natrium-smm-bot/venv/bin"
ExecStart=/opt/natrium-smm-bot/venv/bin/python3 /opt/natrium-smm-bot/src/telegram_bot.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/natrium-smm-bot.log
StandardError=append:/var/log/natrium-smm-bot-error.log

[Install]
WantedBy=multi-user.target
EOF

# Создаем лог-файлы
sudo touch /var/log/natrium-smm-bot.log /var/log/natrium-smm-bot-error.log
sudo chown ubuntu:ubuntu /var/log/natrium-smm-bot*.log

# Настраиваем sudo без пароля для systemctl (нужно для автодеплоя)
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart natrium-smm-bot, /bin/systemctl status natrium-smm-bot, /bin/systemctl is-active natrium-smm-bot" | sudo tee /etc/sudoers.d/natrium-bot

# Запускаем бот
sudo systemctl daemon-reload
sudo systemctl enable natrium-smm-bot
sudo systemctl start natrium-smm-bot

# Проверяем статус
sudo systemctl status natrium-smm-bot
```

### Шаг 2: Настройка GitHub Secrets

Добавьте в **Settings → Secrets and variables → Actions**:

```
ORACLE_SSH_HOST = <IP вашего Oracle Cloud сервера>
ORACLE_SSH_USER = ubuntu
ORACLE_SSH_KEY = <содержимое вашего ~/.ssh/id_rsa>
```

**Важно для `ORACLE_SSH_KEY`:** Скопируйте весь ключ включая:
```
-----BEGIN OPENSSH PRIVATE KEY-----
...весь ключ...
-----END OPENSSH PRIVATE KEY-----
```

### Шаг 3: Деплой!

Теперь каждый раз когда вы пушите в `main` ветку:

```bash
git add .
git commit -m "update: новая версия бота"
git push origin main
```

GitHub Actions автоматически задеплоит изменения на сервер! 🚀

### Проверка деплоя

1. Откройте **Actions** в GitHub репозитории
2. Проверьте статус workflow "Deploy to Oracle Cloud"
3. Или подключитесь к серверу:
   ```bash
   ssh ubuntu@<IP>
   sudo systemctl status natrium-smm-bot
   sudo tail -50 /var/log/natrium-smm-bot.log
   ```

### Полезные команды на сервере

```bash
# Статус бота
sudo systemctl status natrium-smm-bot

# Логи в реальном времени
sudo journalctl -u natrium-smm-bot -f

# Последние 100 строк логов
sudo tail -100 /var/log/natrium-smm-bot.log

# Перезапуск бота
sudo systemctl restart natrium-smm-bot

# Остановка бота
sudo systemctl stop natrium-smm-bot

# Ошибки
sudo tail -100 /var/log/natrium-smm-bot-error.log
```

---

## 📚 Полная документация

См. [DEPLOYMENT.md](./DEPLOYMENT.md) для детальных инструкций и troubleshooting.
