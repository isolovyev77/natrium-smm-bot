# Deployment Guide - Oracle Cloud

## 🎯 Цель
Настроить автоматический деплой Telegram бота на виртуальный сервер Oracle Cloud с workflow: **Codespaces → GitHub → Oracle Cloud**

---

## 📋 Предварительные требования

### На Oracle Cloud сервере
- Ubuntu/Debian Linux
- Python 3.11+
- systemd для автозапуска бота
- SSH доступ с публичным ключом
- Открыт порт для SSH (обычно 22)

### GitHub Secrets (нужно добавить в Settings → Secrets and variables → Actions)
- `ORACLE_SSH_HOST` - IP адрес сервера Oracle Cloud
- `ORACLE_SSH_USER` - пользователь для SSH (обычно `ubuntu` или `opc`)
- `ORACLE_SSH_KEY` - приватный SSH ключ для доступа к серверу
- `TELEGRAM_BOT_TOKEN` - токен Telegram бота
- `YANDEX_FOLDER_ID` - ID папки Yandex Cloud
- `YANDEX_AGENT_ID` - ID агента Yandex Cloud
- `YANDEX_API_KEY` - API ключ Yandex Cloud

---

## 🔧 Шаг 1: Подготовка сервера Oracle Cloud

### 1.1. Подключение к серверу
```bash
ssh ubuntu@<ORACLE_CLOUD_IP>
```

### 1.2. Установка зависимостей
```bash
# Обновление системы
sudo apt update && sudo apt upgrade -y

# Установка Python 3.11 и pip
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Проверка версии
python3.11 --version
```

### 1.3. Создание директории для бота
```bash
# Создаем директорию для бота
sudo mkdir -p /opt/natrium-smm-bot
sudo chown $USER:$USER /opt/natrium-smm-bot
cd /opt/natrium-smm-bot

# Клонируем репозиторий (первый раз вручную)
git clone https://github.com/isolovyev77/natrium-smm-bot.git .

# Создаем виртуальное окружение
python3.11 -m venv venv
source venv/bin/activate

# Устанавливаем зависимости
pip install -r requirements.txt
```

### 1.4. Создание .env файла
```bash
cat > /opt/natrium-smm-bot/.env << 'EOF'
TELEGRAM_BOT_TOKEN=your_token_here
YANDEX_FOLDER_ID=your_folder_id
YANDEX_AGENT_ID=your_agent_id
YANDEX_API_KEY=your_api_key
EOF

# Защищаем файл с секретами
chmod 600 /opt/natrium-smm-bot/.env
```

### 1.5. Создание systemd service
```bash
sudo cat > /etc/systemd/system/natrium-smm-bot.service << 'EOF'
[Unit]
Description=Natrium SMM Bot - Telegram Bot for Social Media Management
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

# Включаем и запускаем сервис
sudo systemctl daemon-reload
sudo systemctl enable natrium-smm-bot
sudo systemctl start natrium-smm-bot

# Проверяем статус
sudo systemctl status natrium-smm-bot
```

### 1.6. Полезные команды для управления ботом
```bash
# Статус бота
sudo systemctl status natrium-smm-bot

# Перезапуск бота
sudo systemctl restart natrium-smm-bot

# Остановка бота
sudo systemctl stop natrium-smm-bot

# Логи в реальном времени
sudo journalctl -u natrium-smm-bot -f

# Последние 100 строк логов
sudo tail -100 /var/log/natrium-smm-bot.log

# Логи ошибок
sudo tail -100 /var/log/natrium-smm-bot-error.log
```

---

## 🚀 Шаг 2: Настройка GitHub Actions для автоматического деплоя

### 2.1. Создание deploy script
Создайте файл `.github/workflows/deploy.yml`:

```yaml
name: Deploy to Oracle Cloud

on:
  push:
    branches:
      - main
  workflow_dispatch:  # Ручной запуск

jobs:
  deploy:
    runs-on: ubuntu-latest
    
    steps:
    - name: Checkout code
      uses: actions/checkout@v4
    
    - name: Setup SSH
      uses: webfactory/ssh-agent@v0.9.0
      with:
        ssh-private-key: ${{ secrets.ORACLE_SSH_KEY }}
    
    - name: Add server to known_hosts
      run: |
        mkdir -p ~/.ssh
        ssh-keyscan -H ${{ secrets.ORACLE_SSH_HOST }} >> ~/.ssh/known_hosts
    
    - name: Deploy to Oracle Cloud
      env:
        SSH_HOST: ${{ secrets.ORACLE_SSH_HOST }}
        SSH_USER: ${{ secrets.ORACLE_SSH_USER }}
        TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
        YANDEX_FOLDER_ID: ${{ secrets.YANDEX_FOLDER_ID }}
        YANDEX_AGENT_ID: ${{ secrets.YANDEX_AGENT_ID }}
        YANDEX_API_KEY: ${{ secrets.YANDEX_API_KEY }}
      run: |
        ssh $SSH_USER@$SSH_HOST << 'ENDSSH'
          set -e
          
          echo "🔄 Updating Natrium SMM Bot..."
          
          # Переход в директорию бота
          cd /opt/natrium-smm-bot
          
          # Сохраняем .env файл
          cp .env .env.backup
          
          # Получаем последние изменения
          git fetch origin main
          git reset --hard origin/main
          
          # Восстанавливаем .env
          mv .env.backup .env
          
          # Обновляем зависимости
          source venv/bin/activate
          pip install -r requirements.txt --quiet
          
          # Перезапускаем сервис
          sudo systemctl restart natrium-smm-bot
          
          # Ждем 5 секунд и проверяем статус
          sleep 5
          sudo systemctl is-active --quiet natrium-smm-bot && echo "✅ Bot started successfully" || echo "❌ Bot failed to start"
          
          echo "✅ Deployment completed!"
        ENDSSH
    
    - name: Verify deployment
      env:
        SSH_HOST: ${{ secrets.ORACLE_SSH_HOST }}
        SSH_USER: ${{ secrets.ORACLE_SSH_USER }}
      run: |
        ssh $SSH_USER@$SSH_HOST "sudo systemctl status natrium-smm-bot --no-pager"
```

### 2.2. Альтернативный вариант с deploy.sh скриптом

Создайте файл `deploy.sh` в корне репозитория:

```bash
#!/bin/bash
set -e

echo "🔄 Deploying Natrium SMM Bot to Oracle Cloud..."

# Переменные окружения (передаются из GitHub Actions)
SSH_HOST="${ORACLE_SSH_HOST}"
SSH_USER="${ORACLE_SSH_USER}"
BOT_DIR="/opt/natrium-smm-bot"

# Деплой через SSH
ssh $SSH_USER@$SSH_HOST << 'ENDSSH'
set -e

echo "📥 Fetching latest changes..."
cd /opt/natrium-smm-bot

# Сохраняем .env
cp .env .env.backup

# Получаем изменения
git fetch origin main
git reset --hard origin/main

# Восстанавливаем .env
mv .env.backup .env

# Обновляем зависимости
source venv/bin/activate
pip install -r requirements.txt --quiet

# Перезапускаем бот
echo "🔄 Restarting bot..."
sudo systemctl restart natrium-smm-bot

# Проверка
sleep 5
if sudo systemctl is-active --quiet natrium-smm-bot; then
    echo "✅ Bot deployed and running successfully!"
    sudo systemctl status natrium-smm-bot --no-pager | head -10
else
    echo "❌ Bot deployment failed!"
    sudo journalctl -u natrium-smm-bot -n 50 --no-pager
    exit 1
fi
ENDSSH

echo "✅ Deployment completed!"
```

Сделайте скрипт исполняемым:
```bash
chmod +x deploy.sh
```

И используйте в `.github/workflows/deploy.yml`:
```yaml
- name: Deploy
  env:
    ORACLE_SSH_HOST: ${{ secrets.ORACLE_SSH_HOST }}
    ORACLE_SSH_USER: ${{ secrets.ORACLE_SSH_USER }}
  run: ./deploy.sh
```

---

## 🔑 Шаг 3: Настройка GitHub Secrets

1. Перейдите в репозиторий на GitHub
2. **Settings** → **Secrets and variables** → **Actions**
3. Нажмите **New repository secret** для каждого секрета:

### SSH доступ
- `ORACLE_SSH_HOST` = IP адрес вашего сервера Oracle Cloud
- `ORACLE_SSH_USER` = `ubuntu` (или `opc` в зависимости от образа)
- `ORACLE_SSH_KEY` = Приватный SSH ключ (содержимое `~/.ssh/id_rsa` или `~/.ssh/id_ed25519`)

### Credentials бота
- `TELEGRAM_BOT_TOKEN` = Токен от @BotFather
- `YANDEX_FOLDER_ID` = ID папки Yandex Cloud
- `YANDEX_AGENT_ID` = ID агента Yandex Cloud
- `YANDEX_API_KEY` = API ключ Yandex Cloud

**Важно:** Для `ORACLE_SSH_KEY` скопируйте **весь** приватный ключ, включая строки:
```
-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----
```

---

## 📝 Шаг 4: Workflow разработки

### Локальная разработка в Codespaces
```bash
# 1. Открываем Codespace
# 2. Редактируем код
# 3. Тестируем локально
python3 src/telegram_bot.py

# 4. Коммитим изменения
git add .
git commit -m "feat: добавлена новая функция"
git push origin main

# 🚀 GitHub Actions автоматически задеплоит изменения на сервер!
```

### Проверка деплоя
1. Откройте **Actions** в репозитории GitHub
2. Найдите последний workflow "Deploy to Oracle Cloud"
3. Проверьте логи выполнения
4. Убедитесь что бот перезапустился успешно

### Откат к предыдущей версии
```bash
# На сервере Oracle Cloud
ssh ubuntu@<IP>
cd /opt/natrium-smm-bot
git log --oneline -10  # Смотрим последние коммиты
git reset --hard <commit_hash>  # Откатываемся к нужному коммиту
sudo systemctl restart natrium-smm-bot
```

---

## 🐛 Troubleshooting

### Проблема: GitHub Actions не может подключиться по SSH
**Решение:**
1. Проверьте что SSH ключ добавлен в GitHub Secrets правильно (с заголовками BEGIN/END)
2. Убедитесь что публичный ключ добавлен на сервер:
   ```bash
   ssh ubuntu@<IP>
   cat ~/.ssh/authorized_keys  # Должен содержать ваш публичный ключ
   ```
3. Проверьте что SSH доступ работает локально:
   ```bash
   ssh -i ~/.ssh/id_rsa ubuntu@<IP>
   ```

### Проблема: Бот не запускается после деплоя
**Решение:**
```bash
# Подключаемся к серверу
ssh ubuntu@<IP>

# Смотрим логи
sudo journalctl -u natrium-smm-bot -n 100 --no-pager

# Проверяем .env файл
cat /opt/natrium-smm-bot/.env

# Проверяем зависимости
cd /opt/natrium-smm-bot
source venv/bin/activate
pip list

# Пробуем запустить вручную
python3 src/telegram_bot.py
```

### Проблема: "Permission denied" при деплое
**Решение:**
```bash
# На сервере дайте права пользователю на sudo без пароля для systemctl
sudo visudo
# Добавьте строку:
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart natrium-smm-bot, /bin/systemctl status natrium-smm-bot
```

### Проблема: Git конфликты при деплое
**Решение:**
```bash
# На сервере
cd /opt/natrium-smm-bot
git fetch origin main
git reset --hard origin/main  # Жесткий сброс до версии из GitHub
sudo systemctl restart natrium-smm-bot
```

---

## 📊 Мониторинг

### Логи в реальном времени
```bash
ssh ubuntu@<IP>
sudo journalctl -u natrium-smm-bot -f
```

### Статус бота
```bash
ssh ubuntu@<IP>
sudo systemctl status natrium-smm-bot
```

### Проверка uptime
```bash
ssh ubuntu@<IP>
sudo systemctl show natrium-smm-bot --property=ActiveState,SubState,ActiveEnterTimestamp
```

---

## 🎉 Готово!

Теперь у вас настроен полный цикл разработки:

1. ✏️ **Редактируем код** в GitHub Codespaces
2. 📤 **Пушим в GitHub** (`git push origin main`)
3. 🤖 **GitHub Actions автоматически деплоит** на Oracle Cloud
4. ✅ **Бот работает 24/7** на боевом сервере

**Workflow команды:**
```bash
# Codespaces
git add .
git commit -m "feat: новая фича"
git push origin main

# GitHub Actions сделает всё остальное автоматически! 🚀
```
