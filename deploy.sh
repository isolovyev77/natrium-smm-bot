#!/bin/bash
set -e

echo "🚀 Deploying Natrium SMM Bot to Oracle Cloud..."

# Проверка переменных окружения
if [ -z "$ORACLE_SSH_HOST" ] || [ -z "$ORACLE_SSH_USER" ]; then
    echo "❌ Error: ORACLE_SSH_HOST and ORACLE_SSH_USER environment variables are required"
    exit 1
fi

SSH_HOST="${ORACLE_SSH_HOST}"
SSH_USER="${ORACLE_SSH_USER}"
BOT_DIR="/opt/natrium-smm-bot"

echo "📡 Connecting to $SSH_USER@$SSH_HOST..."

# Деплой через SSH
ssh $SSH_USER@$SSH_HOST << 'ENDSSH'
set -e

echo "🔄 Updating Natrium SMM Bot..."

# Переход в директорию бота
cd /opt/natrium-smm-bot

# Сохраняем .env файл (содержит секретные токены)
if [ -f .env ]; then
    echo "💾 Backing up .env file..."
    cp .env .env.backup
fi

# Получаем последние изменения из GitHub
echo "📥 Fetching latest changes from GitHub..."
git fetch origin main
git reset --hard origin/main

# Восстанавливаем .env
if [ -f .env.backup ]; then
    echo "📂 Restoring .env file..."
    mv .env.backup .env
fi

# Активируем виртуальное окружение и обновляем зависимости
echo "📦 Installing Python dependencies..."
source venv/bin/activate
pip install -r requirements.txt --quiet

# Перезапускаем systemd сервис
echo "🔄 Restarting bot service..."
sudo systemctl restart natrium-smm-bot

# Ждем 5 секунд для старта бота
echo "⏳ Waiting for bot to start..."
sleep 5

# Проверяем статус бота
if sudo systemctl is-active --quiet natrium-smm-bot; then
    echo "✅ Bot deployed and running successfully!"
    echo ""
    echo "📊 Service status:"
    sudo systemctl status natrium-smm-bot --no-pager | head -10
    exit 0
else
    echo "❌ Bot deployment failed!"
    echo ""
    echo "📋 Recent logs:"
    sudo journalctl -u natrium-smm-bot -n 50 --no-pager
    exit 1
fi
ENDSSH

DEPLOY_STATUS=$?

if [ $DEPLOY_STATUS -eq 0 ]; then
    echo ""
    echo "🎉 Deployment completed successfully!"
    exit 0
else
    echo ""
    echo "💥 Deployment failed with exit code $DEPLOY_STATUS"
    exit $DEPLOY_STATUS
fi
