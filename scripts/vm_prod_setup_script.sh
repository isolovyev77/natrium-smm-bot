#!/bin/bash
# Автоматическая подготовка Production VM для natrium-smm-bot
# Использование: bash vm_prod_setup_script.sh

set -e  # Остановка при ошибках

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   Подготовка Production VM для natrium-smm-bot           ║${NC}"
echo -e "${BLUE}║   Изолированная установка (не мешает другим ботам)       ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Проверка что скрипт НЕ запущен от root
if [ "$EUID" -eq 0 ]; then 
   echo -e "${RED}❌ Не запускайте этот скрипт от root!${NC}"
   echo "Запустите от обычного пользователя (ubuntu, opc и т.д.)"
   exit 1
fi

PROJECT_DIR="/opt/natrium-smm-bot"
SERVICE_NAME="natrium-smm-bot"
REPO_URL="https://github.com/isolovyev77/natrium-smm-bot.git"
SSH_KEY_NAME="github_deploy_natrium"

# ============================================================================
# Шаг 1: Обновление системы и установка зависимостей
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 1/10: Обновление системы ━━━${NC}\n"

sudo apt update -qq
echo -e "${GREEN}✅ Система обновлена${NC}"

# Проверяем наличие Python 3.11
if ! command -v python3.11 &> /dev/null; then
    echo "📦 Установка Python 3.11..."
    sudo apt install -y python3.11 python3.11-venv python3-pip git curl wget >/dev/null 2>&1
    echo -e "${GREEN}✅ Python 3.11 установлен${NC}"
else
    echo -e "${GREEN}✅ Python 3.11 уже установлен${NC}"
fi

# ============================================================================
# Шаг 2: Проверка существующих ботов
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 2/10: Проверка изоляции ━━━${NC}\n"

echo "📊 Запущенные bot-сервисы:"
sudo systemctl list-units --type=service --state=running | grep bot || echo "  Нет других bot-сервисов"

echo ""
echo "📂 Директории в /opt:"
ls -1d /opt/*bot* 2>/dev/null || echo "  Нет других ботов"

echo ""
echo -e "${GREEN}✅ Проверка завершена${NC}"

# ============================================================================
# Шаг 3: Создание директории проекта
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 3/10: Создание директории проекта ━━━${NC}\n"

if [ -d "$PROJECT_DIR" ]; then
    echo -e "${YELLOW}⚠️  Директория $PROJECT_DIR уже существует${NC}"
    echo "Продолжить? Существующие файлы будут сохранены. (y/N)"
    read -r response
    if [[ ! "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        echo "Установка прервана"
        exit 0
    fi
else
    sudo mkdir -p "$PROJECT_DIR"
fi

sudo chown $USER:$USER "$PROJECT_DIR"
echo -e "${GREEN}✅ Директория $PROJECT_DIR готова${NC}"

# ============================================================================
# Шаг 4: Клонирование репозитория
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 4/10: Клонирование репозитория ━━━${NC}\n"

cd "$PROJECT_DIR"

if [ -d ".git" ]; then
    echo "📥 Обновление существующего репозитория..."
    git fetch origin main
    git reset --hard origin/main
else
    echo "📥 Клонирование репозитория..."
    git clone "$REPO_URL" .
fi

echo -e "${GREEN}✅ Репозиторий готов${NC}"

# ============================================================================
# Шаг 5: Создание виртуального окружения
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 5/10: Создание виртуального окружения ━━━${NC}\n"

if [ ! -d "venv" ]; then
    echo "🐍 Создание venv..."
    python3.11 -m venv venv
    echo -e "${GREEN}✅ Виртуальное окружение создано${NC}"
else
    echo -e "${GREEN}✅ Виртуальное окружение уже существует${NC}"
fi

echo "📚 Установка зависимостей..."
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

echo -e "${GREEN}✅ Зависимости установлены${NC}"

# ============================================================================
# Шаг 6: Создание .env файла
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 6/10: Настройка .env файла ━━━${NC}\n"

if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Yandex Cloud Configuration
YANDEX_FOLDER_ID=your_folder_id_here
YANDEX_AGENT_ID=your_agent_id_here
YANDEX_API_KEY=your_api_key_here

# Optional: Logging level
LOG_LEVEL=INFO
EOF
    chmod 600 .env
    echo -e "${GREEN}✅ .env файл создан${NC}"
    echo -e "${YELLOW}⚠️  Не забудьте заполнить .env реальными значениями!${NC}"
    echo -e "${YELLOW}   Или они будут обновлены автоматически через CI/CD${NC}"
else
    echo -e "${GREEN}✅ .env файл уже существует${NC}"
fi

# ============================================================================
# Шаг 7: Создание systemd сервиса
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 7/10: Настройка systemd сервиса ━━━${NC}\n"

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=Natrium SMM Bot - Telegram Bot for Social Media Management
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=$PROJECT_DIR/venv/bin/python3 $PROJECT_DIR/src/telegram_bot.py
Restart=always
RestartSec=10

# Logging (уникальные файлы!)
StandardOutput=append:/var/log/${SERVICE_NAME}.log
StandardError=append:/var/log/${SERVICE_NAME}-error.log

# Security
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}✅ Systemd сервис создан${NC}"

# Создание лог-файлов
sudo touch /var/log/${SERVICE_NAME}.log
sudo touch /var/log/${SERVICE_NAME}-error.log
sudo chown $USER:$USER /var/log/${SERVICE_NAME}*.log
sudo chmod 644 /var/log/${SERVICE_NAME}*.log

echo -e "${GREEN}✅ Лог-файлы созданы${NC}"

# Активация сервиса
sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}

echo -e "${GREEN}✅ Сервис активирован${NC}"

# ============================================================================
# Шаг 8: Создание SSH Deploy Key
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 8/10: Создание SSH Deploy Key ━━━${NC}\n"

if [ -f "$HOME/.ssh/${SSH_KEY_NAME}" ]; then
    echo -e "${YELLOW}⚠️  Deploy Key уже существует${NC}"
    echo "Пересоздать? (y/N)"
    read -r response
    if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        rm -f "$HOME/.ssh/${SSH_KEY_NAME}" "$HOME/.ssh/${SSH_KEY_NAME}.pub"
    else
        echo "Используем существующий ключ"
    fi
fi

if [ ! -f "$HOME/.ssh/${SSH_KEY_NAME}" ]; then
    ssh-keygen -t ed25519 -f "$HOME/.ssh/${SSH_KEY_NAME}" -C "github-deploy-natrium-bot" -N ""
    echo -e "${GREEN}✅ Deploy Key создан${NC}"
else
    echo -e "${GREEN}✅ Deploy Key готов${NC}"
fi

# ============================================================================
# Шаг 9: Настройка Git
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 9/10: Настройка Git ━━━${NC}\n"

cd "$PROJECT_DIR"

# Проверяем remote URL
CURRENT_REMOTE=$(git config --get remote.origin.url || echo "")

if [[ "$CURRENT_REMOTE" == https* ]]; then
    echo "🔄 Меняем HTTPS на SSH..."
    git remote set-url origin git@github.com:isolovyev77/natrium-smm-bot.git
fi

# Настраиваем использование Deploy Key
git config core.sshCommand "ssh -i $HOME/.ssh/${SSH_KEY_NAME} -o IdentitiesOnly=yes"

# Добавляем GitHub в known_hosts
if ! grep -q "github.com" ~/.ssh/known_hosts 2>/dev/null; then
    ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
fi

echo -e "${GREEN}✅ Git настроен на Deploy Key${NC}"

# ============================================================================
# Шаг 10: Настройка sudoers
# ============================================================================

echo -e "\n${YELLOW}━━━ Шаг 10/10: Настройка sudoers ━━━${NC}\n"

sudo tee /etc/sudoers.d/${SERVICE_NAME} > /dev/null << EOF
# Allow $USER to manage ${SERVICE_NAME} service without password
$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart ${SERVICE_NAME}
$USER ALL=(ALL) NOPASSWD: /bin/systemctl stop ${SERVICE_NAME}
$USER ALL=(ALL) NOPASSWD: /bin/systemctl start ${SERVICE_NAME}
$USER ALL=(ALL) NOPASSWD: /bin/systemctl status ${SERVICE_NAME}
$USER ALL=(ALL) NOPASSWD: /bin/journalctl -u ${SERVICE_NAME} *
EOF

sudo chmod 0440 /etc/sudoers.d/${SERVICE_NAME}

# Проверка синтаксиса
if sudo visudo -c -f /etc/sudoers.d/${SERVICE_NAME} >/dev/null 2>&1; then
    echo -e "${GREEN}✅ Sudoers настроен${NC}"
else
    echo -e "${RED}❌ Ошибка в sudoers!${NC}"
    sudo rm -f /etc/sudoers.d/${SERVICE_NAME}
    exit 1
fi

# ============================================================================
# Запуск бота
# ============================================================================

echo -e "\n${YELLOW}━━━ Запуск бота ━━━${NC}\n"

sudo systemctl start ${SERVICE_NAME}
sleep 3

if sudo systemctl is-active --quiet ${SERVICE_NAME}; then
    echo -e "${GREEN}✅ Бот запущен успешно!${NC}"
else
    echo -e "${RED}❌ Бот не запустился. Проверьте логи:${NC}"
    echo "sudo journalctl -u ${SERVICE_NAME} -n 50"
fi

# ============================================================================
# Финальный отчет
# ============================================================================

echo ""
echo -e "${BLUE}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║              🎉 Установка завершена!                     ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

echo -e "${GREEN}✅ Что установлено:${NC}"
echo "  📂 Проект: $PROJECT_DIR"
echo "  🤖 Сервис: ${SERVICE_NAME}.service"
echo "  📝 Логи: /var/log/${SERVICE_NAME}*.log"
echo "  🔑 Deploy Key: ~/.ssh/${SSH_KEY_NAME}"
echo ""

echo -e "${YELLOW}⚠️  Важно! Следующие шаги:${NC}"
echo ""
echo "1️⃣  Заполните .env файл:"
echo "   nano $PROJECT_DIR/.env"
echo ""
echo "2️⃣  Добавьте публичный Deploy Key в GitHub:"
echo "   https://github.com/isolovyev77/natrium-smm-bot/settings/keys"
echo ""
echo -e "${GREEN}   Публичный ключ:${NC}"
cat "$HOME/.ssh/${SSH_KEY_NAME}.pub"
echo ""
echo "3️⃣  Добавьте секреты в GitHub Actions:"
echo "   https://github.com/isolovyev77/natrium-smm-bot/settings/secrets/actions"
echo ""
echo "   DEPLOY_KEY (приватный ключ):"
echo "   cat ~/.ssh/${SSH_KEY_NAME}"
echo ""
echo "   ORACLE_SSH_HOST: $(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_VM_IP')"
echo "   ORACLE_SSH_USER: $USER"
echo "   + токены приложения"
echo ""
echo "4️⃣  Проверьте статус бота:"
echo "   sudo systemctl status ${SERVICE_NAME}"
echo "   sudo journalctl -u ${SERVICE_NAME} -f"
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Показываем статус
sudo systemctl status ${SERVICE_NAME} --no-pager | head -15

echo ""
echo -e "${GREEN}🚀 Production VM готова к работе!${NC}"
