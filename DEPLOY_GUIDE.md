# 🚀 Quick GitHub Deploy Guide

Пошаговая инструкция для публикации проекта в открытом репозитории.

---

## ✅ Чеклист перед публикацией

- [x] `.gitignore` настроен (исключает `.env`, `__pycache__`, `.venv`)
- [x] `.env.example` создан (шаблон без реальных ключей)
- [x] `README.md` полноценный (описание, установка, примеры)
- [x] `LICENSE` добавлена (MIT License)
- [x] `CONTRIBUTING.md` создан (правила для контрибьюторов)
- [x] `requirements.txt` актуален

---

## 📤 Шаги публикации

### 1. Создайте репозиторий на GitHub

1. Перейдите на [github.com/new](https://github.com/new)
2. Заполните:
   - **Repository name**: `natrium-smm-bot`
   - **Description**: "Telegram bot for generating social media posts using Yandex Cloud AI"
   - **Visibility**: ✅ **Public**
   - ❌ НЕ добавляйте README/gitignore/license (они уже есть локально)
3. Нажмите **Create repository**

### 2. Инициализируйте Git локально

```bash
cd /Users/isolovyev/Projects/smm_bot/NatriumSMM

# Если git уже инициализирован, проверьте:
git status

# Если НЕТ git:
git init
git branch -M main
```

### 3. Добавьте remote

```bash
git remote add origin https://github.com/isolovyev77/natrium-smm-bot.git

# Проверьте:
git remote -v
```

### 4. Проверьте, что .env НЕ будет загружен

```bash
git status

# Убедитесь, что в списке НЕТ файла .env
# Если есть - выполните:
git rm --cached .env
```

### 5. Первый commit и push

```bash
# Добавьте все файлы
git add .

# Проверьте список (НЕ должно быть .env, __pycache__, .venv)
git status

# Создайте коммит
git commit -m "Initial commit: Natrium SMM Bot with Yandex AI integration"

# Отправьте на GitHub
git push -u origin main
```

---

## 🔐 Настройка GitHub Secrets

1. Перейдите в ваш репозиторий на GitHub
2. **Settings** → **Secrets and variables** → **Actions**
3. Нажмите **New repository secret**
4. Добавьте секреты:

| Name | Value | Описание |
|------|-------|----------|
| `YANDEX_API_KEY` | `AQVNxxx...` | API ключ Yandex Cloud |
| `YANDEX_FOLDER_ID` | `b1gxxx...` | ID каталога |
| `YANDEX_AGENT_ID` | `fvtxxx...` | ID AI-агента |
| `TELEGRAM_BOT_TOKEN` | `123456:ABC...` | Токен бота (для будущей версии) |

---

## 📋 Что проверить после публикации

- [ ] Репозиторий **Public** (видно в Settings)
- [ ] README отображается на главной странице
- [ ] LICENSE отображается (справа внизу)
- [ ] `.env` **НЕ** видно в файлах
- [ ] `.gitignore` работает корректно

---

## 🎓 Заявка на JetBrains Community Support

### Требования

- ✅ Публичный репозиторий
- ✅ Open-source лицензия (MIT/Apache/GPL)
- ✅ Активная разработка (коммиты)
- ✅ Полезность для сообщества

### Как подать заявку

1. Перейдите на [jetbrains.com/community/opensource](https://www.jetbrains.com/community/opensource/)
2. Нажмите **Apply now**
3. Заполните форму:
   - **Project URL**: `https://github.com/isolovyev77/natrium-smm-bot`
   - **License**: MIT License
   - **Description**: "AI-powered social media content generator for fitness industry"
   - **Active development**: Yes
4. Дождитесь одобрения (обычно 1-2 недели)

---

## 🔄 Обновление репозитория

После локальных изменений:

```bash
git add .
git commit -m "feat: добавлена новая функция X"
git push
```

---

## 🆘 Troubleshooting

### Проблема: `.env` попал в git

```bash
git rm --cached .env
echo ".env" >> .gitignore
git add .gitignore
git commit -m "fix: remove .env from git"
git push --force  # ⚠️ Внимание: удалит историю
```

### Проблема: конфликт при push

```bash
git pull --rebase origin main
git push
```

---

✅ **Готово!** Ваш проект теперь открытый и доступен на GitHub.
