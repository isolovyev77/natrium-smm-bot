# 🖥️ Подготовка Production VM для natrium-smm-bot

## 🎯 Цель

Подготовить виртуальную машину Oracle Cloud для продакшн-деплоя **natrium-smm-bot** с учетом того, что на VM уже работает другой бот. Обеспечить полную изоляцию проектов.

---

## 📋 Предварительные требования

### Информация о существующем боте на VM:
- Путь к существующему боту: `/opt/existing-bot` (пример)
- Имя существующего сервиса: `existing-bot.service`
- Порт (если используется): `8000` (пример)
- Пользователь: `ubuntu` (обычно)

### Требования к системе:
- Ubuntu 20.04+ или Debian 11+
- Python 3.11+ (будет установлен если отсутствует)
- Git
- Минимум 2GB RAM
- Минимум 10GB свободного места

---

## 🔧 Часть 1: Подготовка системы

### 1.1. Обновление системы

```bash
# Обновляем список пакетов
sudo apt update

# Обновляем установленные пакеты (опционально, но рекомендуется)
sudo apt upgrade -y

# Устанавливаем необходимые утилиты
sudo apt install -y \
    python3.11 \
    python3.11-venv \
    python3-pip \
    git \
    curl \
    wget \
    software-properties-common \
    build-essential

# Проверяем версию Python
python3.11 --version
```

**Ожидаемый результат:** Python 3.11.x установлен

---

### 1.2. Проверка существующих ботов

```bash
# Проверяем какие боты уже запущены
sudo systemctl list-units --type=service --state=running | grep bot

# Проверяем используемые порты (если боты используют веб-серверы)
sudo netstat -tulpn | grep LISTEN

# Проверяем занятые директории
ls -la /opt/

# Проверяем процессы Python
ps aux | grep python
```

**Запишите:**
- Какие сервисы уже запущены
- Какие порты заняты
- Какие директории используются

---

## 📁 Часть 2: Создание изолированной структуры проекта

### 2.1. Создание директории проекта

```bash
# Создаем отдельную директорию для natrium-smm-bot
# ⚠️ НЕ конфликтует с существующими ботами в /opt/
sudo mkdir -p /opt/natrium-smm-bot

# Устанавливаем владельца (замените ubuntu на вашего пользователя)
sudo chown $USER:$USER /opt/natrium-smm-bot

# Проверяем права
ls -ld /opt/natrium-smm-bot
```

**Результат:** Директория создана с правами текущего пользователя

---

### 2.2. Клонирование репозитория

```bash
# Переходим в директорию
cd /opt/natrium-smm-bot

# Клонируем репозиторий
git clone https://github.com/isolovyev77/natrium-smm-bot.git .

# Проверяем что файлы скопированы
ls -la

# Проверяем ветку
git branch
```

**Ожидается:**
```
src/
prompts/
data/
requirements.txt
README.md
...
```

---

### 2.3. Создание изолированного виртуального окружения

```bash
# Переходим в директорию проекта
cd /opt/natrium-smm-bot

# Создаем виртуальное окружение с Python 3.11
python3.11 -m venv venv

# Проверяем что venv создан
ls -la venv/

# Активируем виртуальное окружение
source venv/bin/activate

# Проверяем версию Python в venv
python --version
which python

# Обновляем pip
pip install --upgrade pip

# Устанавливаем зависимости проекта
pip install -r requirements.txt

# Деактивируем venv (будет активирован через systemd)
deactivate
```

**Проверка:** 
```bash
venv/bin/python --version  # Должен показать Python 3.11.x
```

---

## 🔐 Часть 3: Настройка конфигурации

### 3.1. Создание .env файла

```bash
cd /opt/natrium-smm-bot

# Создаем .env файл с секретами
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

# Устанавливаем безопасные права (только владелец может читать)
chmod 600 .env

# Проверяем права
ls -l .env
```

**⚠️ ВАЖНО:** 
- Замените `your_bot_token_here` на реальный токен от @BotFather
- Замените Yandex параметры на реальные значения
- `.env` файл НЕ коммитится в Git (добавлен в .gitignore)

---

### 3.2. Проверка директорий для данных

```bash
cd /opt/natrium-smm-bot

# Проверяем структуру директорий
ls -la data/
ls -la output/
ls -la output/posts/

# Создаем недостающие директории если нужно
mkdir -p output/posts/archive

# Устанавливаем права
chmod 755 data/ output/ output/posts/
```

---

## 🔧 Часть 4: Настройка systemd сервиса

### 4.1. Создание уникального systemd service файла

```bash
# Копируем шаблон сервиса
sudo cp /opt/natrium-smm-bot/natrium-smm-bot.service /etc/systemd/system/natrium-smm-bot.service

# Или создаем вручную
sudo tee /etc/systemd/system/natrium-smm-bot.service > /dev/null << 'EOF'
[Unit]
Description=Natrium SMM Bot - Telegram Bot for Social Media Management
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/natrium-smm-bot
Environment="PATH=/opt/natrium-smm-bot/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/opt/natrium-smm-bot/venv/bin/python3 /opt/natrium-smm-bot/src/telegram_bot.py
Restart=always
RestartSec=10

# Logging (УНИКАЛЬНЫЕ файлы логов!)
StandardOutput=append:/var/log/natrium-smm-bot.log
StandardError=append:/var/log/natrium-smm-bot-error.log

# Security
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# ⚠️ Убедитесь что User соответствует вашему пользователю
# Замените ubuntu на вашего пользователя если отличается
```

**Проверка изоляции:**
- ✅ Уникальное имя сервиса: `natrium-smm-bot.service`
- ✅ Уникальная рабочая директория: `/opt/natrium-smm-bot`
- ✅ Уникальное виртуальное окружение: `/opt/natrium-smm-bot/venv`
- ✅ Уникальные лог-файлы: `/var/log/natrium-smm-bot*.log`

---

### 4.2. Создание лог-файлов

```bash
# Создаем файлы логов
sudo touch /var/log/natrium-smm-bot.log
sudo touch /var/log/natrium-smm-bot-error.log

# Устанавливаем владельца (ваш пользователь)
sudo chown $USER:$USER /var/log/natrium-smm-bot.log
sudo chown $USER:$USER /var/log/natrium-smm-bot-error.log

# Устанавливаем права
sudo chmod 644 /var/log/natrium-smm-bot.log
sudo chmod 644 /var/log/natrium-smm-bot-error.log

# Проверяем
ls -l /var/log/natrium-smm-bot*.log
```

---

### 4.3. Активация и запуск сервиса

```bash
# Перезагружаем конфигурацию systemd
sudo systemctl daemon-reload

# Включаем автозапуск при загрузке системы
sudo systemctl enable natrium-smm-bot

# Запускаем сервис
sudo systemctl start natrium-smm-bot

# Ждем 5 секунд для инициализации
sleep 5

# Проверяем статус
sudo systemctl status natrium-smm-bot

# Проверяем что сервис активен
sudo systemctl is-active natrium-smm-bot
```

**Ожидаемый результат:**
```
● natrium-smm-bot.service - Natrium SMM Bot
   Loaded: loaded (/etc/systemd/system/natrium-smm-bot.service; enabled)
   Active: active (running) since ...
```

---

### 4.4. Проверка логов

```bash
# Смотрим последние логи
sudo journalctl -u natrium-smm-bot -n 50 --no-pager

# Или файловые логи
tail -f /var/log/natrium-smm-bot.log
tail -f /var/log/natrium-smm-bot-error.log

# Проверяем что нет ошибок
grep -i error /var/log/natrium-smm-bot-error.log
```

---

## 🔐 Часть 5: Настройка SSH Deploy Key для CI/CD

### 5.1. Создание Deploy Key

```bash
# Создаем ED25519 ключ специально для деплоя
ssh-keygen -t ed25519 \
    -f ~/.ssh/github_deploy_natrium \
    -C "github-deploy-natrium-bot" \
    -N ""

# Проверяем что ключи созданы
ls -la ~/.ssh/github_deploy_natrium*
```

**Результат:**
- `~/.ssh/github_deploy_natrium` - приватный ключ
- `~/.ssh/github_deploy_natrium.pub` - публичный ключ

---

### 5.2. Настройка Git для использования Deploy Key

```bash
cd /opt/natrium-smm-bot

# Проверяем текущий remote
git remote -v

# Если используется HTTPS, меняем на SSH
if git remote get-url origin | grep -q "https://"; then
    git remote set-url origin git@github.com:isolovyev77/natrium-smm-bot.git
    echo "✅ Remote changed to SSH"
fi

# Настраиваем Git использовать наш Deploy Key
git config core.sshCommand "ssh -i ~/.ssh/github_deploy_natrium -o IdentitiesOnly=yes"

# Проверяем настройки
git config --get core.sshCommand
git config --get remote.origin.url

# Добавляем GitHub в known_hosts
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null

# Тестируем подключение (может попросить подтверждения)
ssh -T -i ~/.ssh/github_deploy_natrium git@github.com
```

**Ожидаемый результат:**
```
Hi isolovyev77/natrium-smm-bot! You've successfully authenticated...
```

---

### 5.3. Вывод ключей для GitHub

```bash
echo "=========================================="
echo "📋 ПУБЛИЧНЫЙ КЛЮЧ для GitHub Deploy Keys"
echo "=========================================="
echo ""
echo "Скопируйте и добавьте в:"
echo "https://github.com/isolovyev77/natrium-smm-bot/settings/keys"
echo ""
cat ~/.ssh/github_deploy_natrium.pub
echo ""
echo ""
echo "=========================================="
echo "🔐 ПРИВАТНЫЙ КЛЮЧ для GitHub Secrets"
echo "=========================================="
echo ""
echo "Скопируйте и добавьте в GitHub Secrets как DEPLOY_KEY:"
echo "https://github.com/isolovyev77/natrium-smm-bot/settings/secrets/actions"
echo ""
cat ~/.ssh/github_deploy_natrium
echo ""
```

**Сохраните оба ключа!**

---

## 🛡️ Часть 6: Настройка sudoers для CI/CD

### 6.1. Создание sudoers правил

```bash
# Создаем отдельный файл sudoers для natrium-smm-bot
sudo tee /etc/sudoers.d/natrium-smm-bot > /dev/null << EOF
# Allow $USER to manage natrium-smm-bot service without password
$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart natrium-smm-bot
$USER ALL=(ALL) NOPASSWD: /bin/systemctl stop natrium-smm-bot
$USER ALL=(ALL) NOPASSWD: /bin/systemctl start natrium-smm-bot
$USER ALL=(ALL) NOPASSWD: /bin/systemctl status natrium-smm-bot
$USER ALL=(ALL) NOPASSWD: /bin/journalctl -u natrium-smm-bot *
EOF

# Устанавливаем правильные права
sudo chmod 0440 /etc/sudoers.d/natrium-smm-bot

# Проверяем синтаксис
sudo visudo -c -f /etc/sudoers.d/natrium-smm-bot
```

**Ожидаемый результат:**
```
/etc/sudoers.d/natrium-smm-bot: parsed OK
```

---

### 6.2. Проверка sudoers

```bash
# Должно работать БЕЗ запроса пароля
sudo systemctl status natrium-smm-bot
sudo systemctl restart natrium-smm-bot
sudo journalctl -u natrium-smm-bot -n 5

# Если запрашивает пароль - проверьте файл sudoers
```

---

## 🔍 Часть 7: Проверка изоляции от других ботов

### 7.1. Проверка уникальности компонентов

```bash
# Проверяем что все компоненты уникальны
echo "=== Проверка изоляции ==="
echo ""

echo "✅ Директория проекта:"
ls -ld /opt/natrium-smm-bot
echo ""

echo "✅ Systemd сервис:"
sudo systemctl list-units --type=service | grep bot
echo ""

echo "✅ Лог-файлы:"
ls -lh /var/log/*bot*.log
echo ""

echo "✅ Виртуальное окружение:"
ls -ld /opt/*/venv
echo ""

echo "✅ Python процессы:"
ps aux | grep "[p]ython.*bot" | awk '{print $2, $11, $12, $13}'
echo ""

echo "✅ SSH Deploy Keys:"
ls -la ~/.ssh/github_deploy*
echo ""
```

**Убедитесь что:**
- ✅ Каждый бот в своей директории
- ✅ Каждый бот имеет свой systemd сервис
- ✅ Каждый бот имеет свои лог-файлы
- ✅ Каждый бот имеет свой venv
- ✅ Каждый бот имеет свой Deploy Key (если используется CI/CD)

---

### 7.2. Проверка ресурсов

```bash
# Проверяем использование памяти
free -h

# Проверяем использование диска
df -h

# Проверяем нагрузку процессора
top -b -n 1 | head -20

# Проверяем память используемую ботами
ps aux | grep "[p]ython.*bot" | awk '{sum+=$6} END {print "Total memory:", sum/1024, "MB"}'
```

---

## 📝 Часть 8: Проверка функциональности бота

### 8.1. Тест бота в Telegram

```bash
# Бот должен быть запущен
sudo systemctl is-active natrium-smm-bot

# Смотрим логи в реальном времени
tail -f /var/log/natrium-smm-bot.log
```

**В Telegram:**
1. Найдите бота по username
2. Нажмите `/start`
3. Проверьте что бот отвечает
4. Попробуйте сгенерировать пост

**Одновременно смотрите логи на VM:**
```bash
sudo journalctl -u natrium-smm-bot -f
```

---

### 8.2. Проверка генерации постов

```bash
# Проверяем что посты сохраняются
ls -lh /opt/natrium-smm-bot/output/posts/

# Смотрим последний созданный пост
ls -lt /opt/natrium-smm-bot/output/posts/ | head -5
```

---

## 🚀 Часть 9: Финальная настройка CI/CD

### 9.1. GitHub Secrets (настраивается на GitHub)

Добавьте в **Settings → Secrets → Actions**:

| Secret Name | Value | Где взять |
|-------------|-------|-----------|
| `DEPLOY_KEY` | Приватный SSH ключ | `cat ~/.ssh/github_deploy_natrium` |
| `ORACLE_SSH_HOST` | IP адрес VM | `curl -s ifconfig.me` |
| `ORACLE_SSH_USER` | SSH пользователь | `whoami` |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram | @BotFather |
| `YANDEX_FOLDER_ID` | Yandex Folder ID | Yandex Cloud Console |
| `YANDEX_AGENT_ID` | Yandex Agent ID | Yandex Cloud Console |
| `YANDEX_API_KEY` | Yandex API Key | Yandex Cloud Console |

---

### 9.2. GitHub Deploy Key (настраивается на GitHub)

1. Откройте: https://github.com/isolovyev77/natrium-smm-bot/settings/keys
2. **Add deploy key**
3. **Title:** `Oracle VM Deploy Key`
4. **Key:** Вставьте публичный ключ (`cat ~/.ssh/github_deploy_natrium.pub`)
5. ⚠️ **НЕ** ставьте галочку "Allow write access"
6. **Add key**

---

## ✅ Часть 10: Финальная проверка

### 10.1. Чек-лист готовности

```bash
# Запустите проверку
cd /opt/natrium-smm-bot

echo "=== 🎯 Чек-лист готовности Production VM ==="
echo ""

# 1. Проект склонирован
if [ -d "/opt/natrium-smm-bot/.git" ]; then
    echo "✅ 1. Проект склонирован"
else
    echo "❌ 1. Проект НЕ склонирован"
fi

# 2. Виртуальное окружение создано
if [ -f "/opt/natrium-smm-bot/venv/bin/python" ]; then
    echo "✅ 2. Виртуальное окружение создано"
else
    echo "❌ 2. Виртуальное окружение НЕ создано"
fi

# 3. Зависимости установлены
if /opt/natrium-smm-bot/venv/bin/python -c "import telegram" 2>/dev/null; then
    echo "✅ 3. Зависимости установлены"
else
    echo "❌ 3. Зависимости НЕ установлены"
fi

# 4. .env файл создан
if [ -f "/opt/natrium-smm-bot/.env" ]; then
    echo "✅ 4. .env файл создан"
else
    echo "❌ 4. .env файл НЕ создан"
fi

# 5. Systemd сервис активен
if sudo systemctl is-active --quiet natrium-smm-bot; then
    echo "✅ 5. Systemd сервис активен"
else
    echo "❌ 5. Systemd сервис НЕ активен"
fi

# 6. Deploy Key создан
if [ -f "$HOME/.ssh/github_deploy_natrium" ]; then
    echo "✅ 6. Deploy Key создан"
else
    echo "❌ 6. Deploy Key НЕ создан"
fi

# 7. Git настроен на SSH
if git config --get core.sshCommand | grep -q "github_deploy_natrium"; then
    echo "✅ 7. Git настроен на Deploy Key"
else
    echo "❌ 7. Git НЕ настроен на Deploy Key"
fi

# 8. Sudoers настроен
if sudo -n systemctl status natrium-smm-bot >/dev/null 2>&1; then
    echo "✅ 8. Sudoers настроен"
else
    echo "❌ 8. Sudoers НЕ настроен"
fi

# 9. Логи доступны
if [ -f "/var/log/natrium-smm-bot.log" ]; then
    echo "✅ 9. Лог-файлы созданы"
else
    echo "❌ 9. Лог-файлы НЕ созданы"
fi

# 10. Git pull работает
cd /opt/natrium-smm-bot
if timeout 10 git pull origin main --dry-run >/dev/null 2>&1; then
    echo "✅ 10. Git pull работает"
else
    echo "⚠️  10. Git pull не работает (проверьте Deploy Key на GitHub)"
fi

echo ""
echo "=== Финальная проверка ==="
sudo systemctl status natrium-smm-bot --no-pager | head -10
```

---

### 10.2. Информация для GitHub

```bash
echo ""
echo "=========================================="
echo "📋 Информация для настройки GitHub"
echo "=========================================="
echo ""
echo "🌐 IP адрес VM:"
curl -s ifconfig.me
echo ""
echo ""
echo "👤 SSH пользователь:"
whoami
echo ""
echo "📂 Путь к проекту:"
echo "/opt/natrium-smm-bot"
echo ""
echo "🔑 Публичный Deploy Key (добавить в GitHub Deploy Keys):"
cat ~/.ssh/github_deploy_natrium.pub
echo ""
echo ""
echo "🔐 Приватный Deploy Key (добавить в GitHub Secrets как DEPLOY_KEY):"
echo "Запустите: cat ~/.ssh/github_deploy_natrium"
echo ""
```

---

## 🎉 Готово!

### Что настроено:

✅ **Изолированный проект**
- Уникальная директория: `/opt/natrium-smm-bot`
- Отдельное виртуальное окружение
- Собственный `.env` файл

✅ **Изолированный systemd сервис**
- Уникальное имя: `natrium-smm-bot.service`
- Отдельные лог-файлы
- Автозапуск при перезагрузке

✅ **CI/CD готовность**
- Deploy Key для автоматического деплоя
- Git настроен на SSH
- Sudoers настроен для перезапуска без пароля

✅ **Безопасность**
- .env с правами 600
- Deploy Key изолирован
- Sudoers только для необходимых команд

---

## 📚 Следующие шаги

1. **Добавьте ключи в GitHub:**
   - Deploy Key → Settings → Deploy keys
   - Secrets → Settings → Secrets → Actions

2. **Активируйте CI/CD workflow:**
   ```bash
   # В вашем Codespaces/локально
   cd /workspaces/natrium-smm-bot
   rm .github/workflows/deploy.yml
   mv .github/workflows/deploy-new.yml .github/workflows/deploy.yml
   git add .
   git commit -m "feat: activate production CI/CD"
   git push origin main
   ```

3. **Проверьте автодеплой:**
   - GitHub → Actions → смотрите выполнение workflow

---

## 🐛 Troubleshooting

### Проблема: Конфликт портов
Если оба бота используют веб-серверы, настройте разные порты в `.env`:
```bash
echo "PORT=8001" >> /opt/natrium-smm-bot/.env
```

### Проблема: Нехватка памяти
```bash
# Проверьте доступную память
free -h

# Рассмотрите увеличение swap
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

### Проблема: Бот не запускается
```bash
# Смотрите детальные логи
sudo journalctl -u natrium-smm-bot -n 100 --no-pager

# Попробуйте запустить вручную
cd /opt/natrium-smm-bot
source venv/bin/activate
python src/telegram_bot.py
```

---

**Время настройки:** ~15-20 минут  
**Создано:** 28 января 2026  
**Для:** Codex CLI / Ручная настройка Production VM
