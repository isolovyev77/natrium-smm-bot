# 🏋 Natrium SMM Bot

![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-production-success.svg)
![CI/CD](https://img.shields.io/badge/CI%2FCD-automated-blue.svg)

**Автоматический Telegram-бот для создания качественного контента с использованием Yandex Cloud AI Agent.**

Бот использует **Yandex Foundation Models (YandexGPT)** и технику CoV+CoK (Chain of Verification + Chain of Knowledge) для генерации экспертных постов о фитнесе, CrossFit и здоровье для зала Натриум Фитнесс.

---

## ✨ Основные возможности

### 🤖 Telegram интерфейс
- **Генерация тем** — 10 актуальных идей с источниками одной кнопкой
- **Фокусированный поиск** — темы по категориям: 🏋 Спорт, 🥗 Питание, 😴 Сон, 💪 Техника, 🔬 Наука, 🎲 Разное
- **Генерация постов** — готовый контент с заголовком, лидом, секциями, CTA и хештегами
- **Настройка длины** — от 200 до 1000 символов
- **История** — просмотр всех сгенерированных постов

### 🧠 AI технологии
- **CoV+CoK** — Chain-of-Verification + Chain-of-Knowledge для проверки фактов
- **Web Search** — актуальные данные 2026 года (CrossFit Open, Games, исследования)
- **File Search** — база знаний из книг по физиологии, программированию тренировок, CrossFit Level 1/2
- **Smart Prompting** — оптимизированные промпты для генерации вирусного контента

### 📝 Качество контента
- **Структура** — обязательный заголовок и лид-затравка для каждого поста
- **Источники** — ссылки на PubMed, ВОЗ, CrossFit.com, научные исследования
- **HTML форматирование** — кликабельные ссылки в Telegram
- **Emoji и буллеты** — визуально привлекательный формат
- **Хештеги** — релевантные теги для каждого поста

### 🚀 CI/CD автоматизация
- **GitHub Actions** — автодеплой при push в main
- **Oracle Cloud VM** — production хостинг с systemd
- **Health checks** — мониторинг работы бота
- **Rolling updates** — обновления без даунтайма

---

## 🚀 Быстрый деплой

### Production на Oracle Cloud (рекомендуется)
- ⚡ [Быстрая установка VM](./VM_QUICK_INSTALL.md) — одна команда, 5-10 минут
- 📋 [Полная инструкция VM](./VM_PRODUCTION_SETUP.md) — детальная подготовка

### Настройка CI/CD
- ⚡ [Быстрая настройка CI/CD](./QUICK_CICD_SETUP.md) — настройка за 5 минут
- 🤖 [Инструкции для Copilot](./COPILOT_CICD_SETUP.md) — подробная настройка с Deploy Key
- 📚 [Навигация по документации](./CICD_INDEX.md) — все документы CI/CD

### Альтернативные варианты
- 📖 [Быстрый старт деплоя](./DEPLOY_QUICK.md) — ручной деплой
- 📚 [Полная документация](./DEPLOYMENT.md) — детальные инструкции

**Workflow:** GitHub Codespaces → GitHub Actions → Oracle VM (автоматически)

---

## 🏁 Быстрый старт

### 1. Получите API ключи

**Yandex Cloud:**
1. Перейдите в [Yandex Cloud Console](https://console.yandex.cloud)
2. Создайте API ключ в разделе **Сервисные аккаунты**
3. Создайте AI Agent в **Foundation Models** → **Agents**
4. Загрузите системный промпт из [prompts/agent_system_prompt.md](prompts/agent_system_prompt.md)
5. Создайте File Search индекс и загрузите PDF из `data/`

**Telegram:**
1. Откройте [@BotFather](https://t.me/botfather)
2. Создайте бота командой `/newbot`
3. Сохраните токен

### 2. Локальный запуск (разработка)

```bash
# Клонирование
git clone https://github.com/isolovyev77/natrium-smm-bot.git
cd natrium-smm-bot

# Виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Зависимости
pip install -r requirements.txt

# Настройка .env
cp .env.example .env
# Отредактируйте .env своими ключами

# Запуск бота
python src/telegram_bot.py
```

### 3. Production деплой (Oracle Cloud)

Полная инструкция: [VM_QUICK_INSTALL.md](./VM_QUICK_INSTALL.md)

```bash
# На VM выполните автоматическую установку
curl -fsSL https://raw.githubusercontent.com/isolovyev77/natrium-smm-bot/main/scripts/vm_prod_setup_script.sh | bash
```

Скрипт автоматически:
- Установит зависимости (Python, Git, systemd)
- Склонирует репозиторий
- Настроит .env (интерактивно)
- Запустит бота как systemd сервис
- Настроит автозапуск при перезагрузке

---

## 📂 Структура проекта

```
natrium-smm-bot/
├── src/                        # Исходный код
│   ├── telegram_bot.py        # Telegram бот (главный файл)
│   ├── bot.py                 # Yandex API клиент
│   ├── config.py              # Конфигурация
│   ├── prompts.py             # Промпты для генерации
│   └── parsers/               # Парсеры ответов API
│       └── local_parser.py
├── prompts/                   # Системные промпты
│   ├── agent_system_prompt.md # Основной промпт для Agent
│   └── README.md
├── data/                      # База знаний (PDF)
│   └── sources.md            # Список источников
├── output/posts/             # Сгенерированные посты
│   └── archive/              # Архив постов
├── scripts/                  # Скрипты деплоя
│   ├── vm_prod_setup_script.sh
│   └── setup_deploy_key.sh
├── .github/workflows/        # CI/CD
│   └── deploy.yml
├── .env.example              # Шаблон переменных окружения
├── requirements.txt          # Python зависимости
├── natrium-smm-bot.service  # systemd unit
└── README.md                # Эта документация
```

---

## 🎯 Использование Telegram бота

### Основные команды

- `/start` — начало работы, показывает главное меню
- `/help` — справка по использованию
- `/history` — просмотр всех сгенерированных постов

### Процесс создания поста

1. **Генерация тем** — нажмите "📝 Сгенерировать темы"
   - Выберите фокус: 🏋 Спорт, 🥗 Питание, 😴 Сон, 💪 Техника, 🔬 Наука, 🎲 Разное
   - Получите 10 актуальных идей с источниками

2. **Выбор темы** — нажмите на интересующую тему
   - Или введите свою тему текстом

3. **Настройка длины** — выберите формат:
   - 📏 Короткий (200-300 символов)
   - 📄 Средний (400-600 символов)
   - 📰 Длинный (700-1000 символов)

4. **Получение поста** — бот сгенерирует готовый контент
   - Заголовок в CAPS с эмодзи
   - Лид-затравка для привлечения внимания
   - Секции с фактами и источниками
   - CTA и хештеги

### Пример сгенерированного поста

```
🔥 **РЕГЕНЕРАЦИЯ: СЕКРЕТ ПОСТОЯННОГО ПРОГРЕССА**

Знакомо: после убойной тренировки не можешь пошевелиться два дня? 
Многие думают, что это нормально. На самом деле правильное 
восстановление - это 50% успеха.

🔥 **ФАКТОРЫ РЕГЕНЕРАЦИИ:**
• Качественный сон 7-9 часов — время роста мышц (WHO)
• Питание в течение 30 минут после — закрытие углеводного окна
• Активное восстановление — легкое кардио ускоряет вывод лактата

💓 **В НАТРИУМ:**
Используем протоколы восстановления от CrossFit HQ:
✅ Foam rolling после WOD
✅ Стретчинг 10-15 минут
✅ Контроль пульса при восстановлении

Восстанавливайся правильно — прогрессируй быстрее! 🔥

#натриумфитнес #crossfit #восстановление #регенерация
```

---

## � Технологии и архитектура

### Backend
- **Python 3.12+** — основной язык
- **httpx** — асинхронные HTTP запросы к Yandex API
- **python-telegram-bot** — Telegram Bot API
- **python-dotenv** — управление переменными окружения

### AI платформа
- **Yandex Foundation Models** — YandexGPT для генерации текста
- **Yandex AI Agent** — оркестрация промптов и инструментов
- **Web Search** — актуальная информация из интернета
- **File Search** — поиск по базе знаний (PDF документы)

### DevOps
- **GitHub Actions** — CI/CD pipeline
- **Oracle Cloud** — production хостинг (Always Free Tier)
- **systemd** — управление сервисом
- **SSH Deploy** — безопасный деплой через SSH ключи

### Промпт-инжиниринг
- **CoV (Chain-of-Verification)** — верификация фактов через источники
- **CoK (Chain-of-Knowledge)** — связывание знаний из базы
- **Structured prompts** — четкая структура ввода/вывода
- **Few-shot learning** — обучение на примерах постов

---

## 🔧 Конфигурация

### Переменные окружения (.env)

```env
# Yandex Cloud
YANDEX_FOLDER_ID=b1g...         # ID каталога
YANDEX_API_KEY=AQVN...          # API ключ сервисного аккаунта
YANDEX_AGENT_ID=fvt...          # ID AI Agent

# Telegram
TELEGRAM_BOT_TOKEN=1234567890:ABC...  # Токен от @BotFather

# Опционально
LOG_LEVEL=INFO                  # DEBUG, INFO, WARNING, ERROR
```

### Системный промпт

Основной промпт находится в [prompts/agent_system_prompt.md](prompts/agent_system_prompt.md).

**Ключевые элементы:**
- Роль эксперта по CrossFit и физиологии
- Обязательная структура: заголовок → лид → секции → CTA
- Требование источников для всех фактов
- Форматирование: эмодзи, буллеты, **жирный текст**
- Хештеги и призыв к действию

**Обновление промпта:**
```python
from src.bot import NatriumBot
bot = NatriumBot()
bot.update_agent_prompt()  # Загрузит из prompts/agent_system_prompt.md
```

---

## 📊 Мониторинг и логи

### Просмотр логов (systemd)

```bash
# Последние 100 строк
journalctl -u natrium-smm-bot -n 100

# Следить за логами в реальном времени
journalctl -u natrium-smm-bot -f

# Логи за сегодня
journalctl -u natrium-smm-bot --since today
```

### Управление сервисом

```bash
# Статус
sudo systemctl status natrium-smm-bot

# Перезапуск
sudo systemctl restart natrium-smm-bot

# Остановка
sudo systemctl stop natrium-smm-bot

# Запуск
sudo systemctl start natrium-smm-bot
```

### Проверка работоспособности

```bash
# Проверка подключения к API
cd /opt/natrium-smm-bot
source venv/bin/activate
python -c "from src.bot import NatriumBot; bot = NatriumBot(); print('✅ OK')"
```

---

## 🤝 Участие в разработке

Приветствуем ваш вклад! См. [CONTRIBUTING.md](./CONTRIBUTING.md) для деталей.

### Процесс
1. Fork репозитория
2. Создайте ветку: `git checkout -b feature/amazing-feature`
3. Commit изменений: `git commit -m 'feat: add amazing feature'`
4. Push в ветку: `git push origin feature/amazing-feature`
5. Откройте Pull Request

### Changelog
Все изменения документируются в [CHANGELOG.md](./CHANGELOG.md).

---

## 📝 Документация

- 📖 [README.md](./README.md) — основная документация (этот файл)
- 🚀 [DEPLOYMENT.md](./DEPLOYMENT.md) — детальные инструкции по деплою
- ⚡ [QUICK_CICD_SETUP.md](./QUICK_CICD_SETUP.md) — быстрая настройка CI/CD
- 🏗️ [CICD_ARCHITECTURE.md](./CICD_ARCHITECTURE.md) — архитектура CI/CD
- 📊 [CHANGELOG.md](./CHANGELOG.md) — история изменений
- 🤝 [CONTRIBUTING.md](./CONTRIBUTING.md) — гайд для контрибьюторов

---

## 📄 Лицензия

Проект распространяется под лицензией **MIT**. См. [LICENSE](./LICENSE).

---

## 👨‍💻 Автор

**Ivan Solovyev** ([@isolovyev77](https://github.com/isolovyev77))

Проект создан для автоматизации SMM контента фитнес-зала Натриум Фитнесс.

---

## 🙏 Благодарности

- [Yandex Cloud](https://yandex.cloud) — Foundation Models API
- [CrossFit](https://crossfit.com) — методология тренировок
- [Oracle Cloud](https://www.oracle.com/cloud/free/) — бесплатный хостинг
- Натриум Фитнесс — вдохновение и поддержка

---

## 📞 Поддержка

- **Issues**: [GitHub Issues](https://github.com/isolovyev77/natrium-smm-bot/issues)
- **Discussions**: [GitHub Discussions](https://github.com/isolovyev77/natrium-smm-bot/discussions)

---

*Последнее обновление: февраль 2026*
