#!/bin/bash
# Скрипт для быстрой настройки Deploy Key на Oracle VM
# Запустите этот скрипт на вашем Oracle VM сервере

set -e

echo "🔐 Настройка SSH Deploy Key для автоматического деплоя"
echo "========================================================"
echo ""

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Проверка что скрипт запущен не от root
if [ "$EUID" -eq 0 ]; then 
   echo -e "${RED}❌ Не запускайте этот скрипт от root!${NC}"
   echo "Запустите от обычного пользователя (ubuntu, opc и т.д.)"
   exit 1
fi

echo -e "${YELLOW}Шаг 1/5: Создание SSH Deploy Key${NC}"
if [ -f ~/.ssh/github_deploy_key ]; then
    echo "⚠️  Deploy key уже существует. Пересоздать? (y/N)"
    read -r response
    if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        rm ~/.ssh/github_deploy_key ~/.ssh/github_deploy_key.pub
    else
        echo "Используем существующий ключ"
    fi
fi

if [ ! -f ~/.ssh/github_deploy_key ]; then
    ssh-keygen -t ed25519 -f ~/.ssh/github_deploy_key -C "github-deploy-natrium-bot" -N ""
    echo -e "${GREEN}✅ SSH ключ создан${NC}"
else
    echo -e "${GREEN}✅ SSH ключ уже существует${NC}"
fi

echo ""
echo -e "${YELLOW}Шаг 2/5: Настройка Git для использования Deploy Key${NC}"

PROJECT_DIR="/opt/natrium-smm-bot"

if [ ! -d "$PROJECT_DIR" ]; then
    echo -e "${RED}❌ Проект не найден в $PROJECT_DIR${NC}"
    echo "Сначала клонируйте репозиторий:"
    echo "  sudo mkdir -p $PROJECT_DIR"
    echo "  sudo chown \$USER:\$USER $PROJECT_DIR"
    echo "  git clone https://github.com/isolovyev77/natrium-smm-bot.git $PROJECT_DIR"
    exit 1
fi

cd "$PROJECT_DIR"

# Проверяем remote URL
CURRENT_REMOTE=$(git config --get remote.origin.url || echo "")

if [[ "$CURRENT_REMOTE" == https* ]]; then
    echo "🔄 Меняем HTTPS на SSH remote..."
    git remote set-url origin git@github.com:isolovyev77/natrium-smm-bot.git
    echo -e "${GREEN}✅ Remote URL обновлен на SSH${NC}"
else
    echo -e "${GREEN}✅ Remote уже использует SSH${NC}"
fi

# Настраиваем Git использовать наш ключ
git config core.sshCommand "ssh -i ~/.ssh/github_deploy_key -o IdentitiesOnly=yes"
echo -e "${GREEN}✅ Git настроен для использования Deploy Key${NC}"

# Добавляем github.com в known_hosts если его там нет
if ! grep -q "github.com" ~/.ssh/known_hosts 2>/dev/null; then
    echo "📝 Добавляем GitHub в known_hosts..."
    ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
fi

echo ""
echo -e "${YELLOW}Шаг 3/5: Проверка Git подключения${NC}"
if git pull origin main --dry-run 2>&1 | grep -q "up to date\|Already up to date\|Would merge"; then
    echo -e "${GREEN}✅ Git подключение работает!${NC}"
else
    echo -e "${RED}❌ Git подключение не работает${NC}"
    echo "Убедитесь что публичный ключ добавлен в GitHub Deploy Keys"
fi

echo ""
echo -e "${YELLOW}Шаг 4/5: Настройка sudoers для systemctl${NC}"

SUDOERS_LINE="$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart natrium-smm-bot, /bin/systemctl stop natrium-smm-bot, /bin/systemctl start natrium-smm-bot, /bin/systemctl status natrium-smm-bot, /bin/journalctl -u natrium-smm-bot *"

# Проверяем есть ли уже правило
if sudo grep -q "natrium-smm-bot" /etc/sudoers.d/* 2>/dev/null || sudo grep -q "natrium-smm-bot" /etc/sudoers 2>/dev/null; then
    echo -e "${GREEN}✅ sudoers уже настроен${NC}"
else
    echo "Требуется добавить правило в sudoers для перезапуска сервиса без пароля"
    echo "Будет выполнена команда: sudo visudo"
    echo ""
    echo "Добавьте эту строку в конец файла:"
    echo -e "${YELLOW}$SUDOERS_LINE${NC}"
    echo ""
    echo "Нажмите Enter чтобы продолжить..."
    read -r

    # Создаем файл sudoers для нашего сервиса
    echo "$SUDOERS_LINE" | sudo tee /etc/sudoers.d/natrium-smm-bot > /dev/null
    sudo chmod 0440 /etc/sudoers.d/natrium-smm-bot
    echo -e "${GREEN}✅ sudoers настроен${NC}"
fi

# Проверяем что sudoers работает
if sudo -n systemctl status natrium-smm-bot &>/dev/null; then
    echo -e "${GREEN}✅ systemctl работает без пароля${NC}"
else
    echo -e "${RED}⚠️  systemctl все еще требует пароль${NC}"
    echo "Попробуйте выйти и зайти снова для применения изменений"
fi

echo ""
echo -e "${YELLOW}Шаг 5/5: Проверка systemd сервиса${NC}"

if sudo systemctl is-enabled natrium-smm-bot &>/dev/null; then
    echo -e "${GREEN}✅ Сервис natrium-smm-bot установлен и включен${NC}"
    sudo systemctl status natrium-smm-bot --no-pager | head -5
else
    echo -e "${YELLOW}⚠️  Сервис не установлен. Устанавливаем...${NC}"
    if [ -f "$PROJECT_DIR/natrium-smm-bot.service" ]; then
        sudo cp "$PROJECT_DIR/natrium-smm-bot.service" /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable natrium-smm-bot
        sudo systemctl start natrium-smm-bot
        echo -e "${GREEN}✅ Сервис установлен и запущен${NC}"
    else
        echo -e "${RED}❌ Файл сервиса не найден${NC}"
    fi
fi

echo ""
echo "=========================================="
echo -e "${GREEN}🎉 Настройка завершена!${NC}"
echo "=========================================="
echo ""
echo -e "${YELLOW}📋 Следующие шаги:${NC}"
echo ""
echo "1️⃣  Скопируйте ПУБЛИЧНЫЙ ключ и добавьте в GitHub Deploy Keys:"
echo "   https://github.com/isolovyev77/natrium-smm-bot/settings/keys"
echo ""
echo -e "${GREEN}cat ~/.ssh/github_deploy_key.pub${NC}"
cat ~/.ssh/github_deploy_key.pub
echo ""
echo "2️⃣  Скопируйте ПРИВАТНЫЙ ключ и добавьте в GitHub Secrets как DEPLOY_KEY:"
echo "   https://github.com/isolovyev77/natrium-smm-bot/settings/secrets/actions"
echo ""
echo -e "${GREEN}cat ~/.ssh/github_deploy_key${NC}"
cat ~/.ssh/github_deploy_key
echo ""
echo "3️⃣  Добавьте остальные секреты в GitHub:"
echo "   - ORACLE_SSH_HOST = $(curl -s ifconfig.me)"
echo "   - ORACLE_SSH_USER = $USER"
echo "   - TELEGRAM_BOT_TOKEN = (ваш токен)"
echo "   - YANDEX_FOLDER_ID = (ваш folder id)"
echo "   - YANDEX_AGENT_ID = (ваш agent id)"
echo "   - YANDEX_API_KEY = (ваш api key)"
echo ""
echo "4️⃣  Закоммитьте изменения в репозиторий и проверьте автодеплой!"
echo ""
echo -e "${GREEN}✅ Готово! Автоматический деплой настроен!${NC}"
