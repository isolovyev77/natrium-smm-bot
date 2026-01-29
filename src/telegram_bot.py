import os
import sys
import logging
import atexit
import fcntl
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.append(str(Path(__file__).parent.parent))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from src.bot import NatriumBot
from src.config import TELEGRAM_BOT_TOKEN

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# PID файл для предотвращения множественных запусков
PID_FILE = Path("/tmp/natrium-smm-bot.pid")
LOCK_FILE = None


def acquire_lock():
    """Получить эксклюзивную блокировку для предотвращения множественных запусков"""
    global LOCK_FILE
    
    try:
        # Открываем файл блокировки
        LOCK_FILE = open(PID_FILE, 'w')
        
        # Пытаемся получить эксклюзивную блокировку (неблокирующий режим)
        fcntl.flock(LOCK_FILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        
        # Записываем PID текущего процесса
        LOCK_FILE.write(str(os.getpid()))
        LOCK_FILE.flush()
        
        logger.info(f"🔒 Блокировка получена, PID: {os.getpid()}")
        return True
        
    except IOError:
        # Не удалось получить блокировку - другой экземпляр уже запущен
        if LOCK_FILE:
            LOCK_FILE.close()
        
        # Читаем PID другого процесса
        try:
            with open(PID_FILE, 'r') as f:
                other_pid = f.read().strip()
                logger.error(f"❌ ОШИБКА: Другой экземпляр бота уже запущен (PID: {other_pid})")
                print(f"\n❌ ОШИБКА: Другой экземпляр natrium-smm-bot уже запущен!")
                print(f"   PID запущенного процесса: {other_pid}")
                print(f"\nЧтобы остановить его, выполните:")
                print(f"   sudo systemctl stop natrium-smm-bot")
                print(f"   или: kill {other_pid}\n")
        except:
            logger.error("❌ ОШИБКА: Другой экземпляр бота уже запущен")
            print("\n❌ ОШИБКА: Другой экземпляр natrium-smm-bot уже запущен!\n")
        
        return False


def release_lock():
    """Освободить блокировку при выходе"""
    global LOCK_FILE
    
    if LOCK_FILE:
        try:
            fcntl.flock(LOCK_FILE.fileno(), fcntl.LOCK_UN)
            LOCK_FILE.close()
            
            # Удаляем PID файл
            if PID_FILE.exists():
                PID_FILE.unlink()
            
            logger.info("🔓 Блокировка освобождена")
        except Exception as e:
            logger.error(f"Ошибка освобождения блокировки: {e}")


# Регистрируем функцию очистки при выходе
atexit.register(release_lock)


class TelegramSMMBot:
    def __init__(self):
        if not TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN не найден в переменных окружения. Проверьте GitHub Secrets.")
        
        self.natrium_bot = NatriumBot()
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Постоянная клавиатура с кнопкой /start
        self.main_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("🔄 Начать заново")]],
            resize_keyboard=True,
            one_time_keyboard=False
        )
        
        # Регистрация обработчиков
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_handler))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        
        # Устанавливаем оптимальную технику cov+cok
        technique = 'cov+cok'
        context.user_data['technique'] = technique
        
        welcome_text = f"""
🤖 **Привет, {user.first_name}!**

Я бот для генерации контента для Натриум Фитнесс.

🎯 **Мои возможности:**
• Генерация актуальных тем для постов
• Создание готовых постов с эмодзи и хештегами
• Проверенные факты из научных источников

📚 **База знаний:**
• CrossFit методики
• Исследования ВОЗ и PubMed
• Книга о соцсетях
"""
        
        await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=self.main_keyboard)
        
        # Показываем выбор фокуса вместо сразу генерации тем
        focus_text = "🎯 *НА ЧТО СДЕЛАТЬ УПОР В ТЕМАХ?*\n\nВыберите направление:"
        
        keyboard = [
            [InlineKeyboardButton("🍽️ Питание и диета", callback_data="focus_nutrition")],
            [InlineKeyboardButton("💪 Спорт и тренировки (CrossFit, силовые)", callback_data="focus_sport")],
            [InlineKeyboardButton("💤 Сон и восстановление", callback_data="focus_sleep")],
            [InlineKeyboardButton("🤸 Техника упражнений (гимнастика, атлетика)", callback_data="focus_technique")],
            [InlineKeyboardButton("🏥 Здоровье и профилактика (ВОЗ)", callback_data="focus_health")],
            [InlineKeyboardButton("🎲 Разное (без фокуса)", callback_data="focus_random")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(focus_text, reply_markup=reply_markup, parse_mode='Markdown')

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        # Выбор темы по номеру
        if data.startswith("theme_"):
            theme_num = int(data.replace("theme_", ""))
            parsed_themes = context.user_data.get('parsed_themes', [])
            
            logger.info(f"theme_ handler: выбрана тема {theme_num}, всего тем: {len(parsed_themes)}")
            
            if 1 <= theme_num <= len(parsed_themes):
                theme_name = parsed_themes[theme_num - 1]
                context.user_data['current_theme'] = theme_name
                
                # Запрашиваем длину поста (без названия темы в callback)
                keyboard = [
                    [InlineKeyboardButton("📏 500 символов", callback_data="len_500")],
                    [InlineKeyboardButton("📏 700 символов", callback_data="len_700")],
                    [InlineKeyboardButton("📏 1000 символов", callback_data="len_1000")],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    f"✅ Тема: **{theme_name}**\n\nВыберите длину поста:",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
        
        # Пользователь хочет написать свою тему
        elif data == "custom_theme":
            await query.edit_message_text(
                "✏️ Напишите свою тему для поста:",
                parse_mode='Markdown'
            )
            context.user_data['waiting_custom_theme'] = True
        
        # Выбор длины поста
        elif data.startswith("len_"):
            post_length = int(data.replace("len_", ""))
            theme_name = context.user_data.get('current_theme', '')
            technique = context.user_data.get('technique', 'cov+cok')
            
            if not theme_name:
                await query.edit_message_text(
                    "❌ Ошибка: тема не выбрана. Используйте /start",
                    parse_mode='Markdown'
                )
                return
            
            await self.generate_post_callback(query, theme_name, technique, post_length)
        
        # Регенерация поста (используем current_theme из контекста)
        elif data == "regen":
            theme_name = context.user_data.get('current_theme', '')
            
            if not theme_name:
                await query.edit_message_text(
                    "❌ Ошибка: тема не найдена. Используйте /start",
                    parse_mode='Markdown'
                )
                return
            
            # Запрашиваем длину поста
            keyboard = [
                [InlineKeyboardButton("📏 500 символов", callback_data="len_500")],
                [InlineKeyboardButton("📏 700 символов", callback_data="len_700")],
                [InlineKeyboardButton("📏 1000 символов", callback_data="len_1000")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"✅ Тема: **{theme_name}**\n\nВыберите длину поста:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        
        # Другая тема - показываем УЖЕ сгенерированные темы
        elif data == "other_theme":
            parsed_themes = context.user_data.get('parsed_themes', [])
            
            if not parsed_themes:
                await query.edit_message_text(
                    "❌ Темы не найдены. Используйте /start",
                    parse_mode='Markdown'
                )
                return
            
            # Формируем сообщение БЕЗ перечисления тем
            bulb = chr(0x1F4A1)  # 💡
            themes_text = (
                f"{bulb} Выберите тему:\n\n"
                f"_Длинная тема → 🔄📱_"
            )
            
            # Создаём кнопки для ВСЕХ найденных тем
            keyboard = []
            for i, theme in enumerate(parsed_themes, 1):
                # Нормализуем регистр: первая буква заглавная
                normalized_theme = theme.capitalize()
                # Добавляем номер темы перед текстом
                button_text = f"{i}. {normalized_theme}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"theme_{i}")])
            
            keyboard.append([InlineKeyboardButton("✏️ Написать свою тему", callback_data="custom_theme")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(themes_text, reply_markup=reply_markup, parse_mode='Markdown')
        
        # Новые темы - показываем выбор фокуса
        elif data == "new_themes":
            # Показываем кнопки выбора фокуса
            focus_text = "🎯 *НА ЧТО СДЕЛАТЬ УПОР В НОВЫХ ТЕМАХ?*\n\nВыберите направление:"
            
            keyboard = [
                [InlineKeyboardButton("🍽️ Питание и диета", callback_data="focus_nutrition")],
                [InlineKeyboardButton("💪 Спорт и тренировки (CrossFit, силовые)", callback_data="focus_sport")],
                [InlineKeyboardButton("💤 Сон и восстановление", callback_data="focus_sleep")],
                [InlineKeyboardButton("🤸 Техника упражнений (гимнастика, атлетика)", callback_data="focus_technique")],
                [InlineKeyboardButton("🏥 Здоровье и профилактика (ВОЗ)", callback_data="focus_health")],
                [InlineKeyboardButton("🎲 Разное (без фокуса)", callback_data="focus_random")]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(focus_text, reply_markup=reply_markup, parse_mode='Markdown')
        
        # Обработка выбора фокуса для новых тем
        elif data.startswith("focus_"):
            focus_type = data.replace("focus_", "")
            
            # Мапинг фокуса на ключевые слова
            focus_map = {
                "nutrition": "питание, диета, спортивное питание",
                "sport": "спорт, тренировки, CrossFit, силовые упражнения, меткон",
                "sleep": "сон, восстановление, регенерация",
                "technique": "техника упражнений, гимнастика, олимпийская атлетика, прогрессии",
                "health": "здоровье, профилактика, рекомендации ВОЗ, научные исследования",
                "random": None  # без фокуса
            }
            
            focus_keywords = focus_map.get(focus_type)
            technique = context.user_data.get('technique', 'cov+cok')
            
            await query.edit_message_text(
                "🔄 Генерирую новые темы...",
                parse_mode='Markdown'
            )
            
            try:
                # Формируем custom_input с фокусом
                if focus_keywords:
                    custom_input = f"Сгенерируй 10 актуальных тем для постов с ФОКУСОМ НА: {focus_keywords}. Обязательно используй разнообразные форматы из книги о соцсетях!"
                else:
                    custom_input = None
                
                themes, usage = self.natrium_bot.generate_themes(technique, custom_input=custom_input)
                context.user_data['themes'] = themes
                
                # Парсим темы и создаём кнопки
                parsed_themes = self.parse_themes_list(themes)
                context.user_data['parsed_themes'] = parsed_themes
                
                logger.info(f"focus_{focus_type}: распарсено {len(parsed_themes)} тем для кнопок")
                
                # Формируем сообщение БЕЗ перечисления тем
                bulb = chr(0x1F4A1)  # 💡
                themes_text = (
                    f"{bulb} Выберите тему:\n\n"
                    f"_Длинная тема → 🔄📱_"
                )
                
                # Создаём кнопки для ВСЕХ найденных тем
                keyboard = []
                for i, theme in enumerate(parsed_themes, 1):
                    # Нормализуем регистр: первая буква заглавная
                    normalized_theme = theme.capitalize()
                    # Добавляем номер темы перед текстом
                    button_text = f"{i}. {normalized_theme}"
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=f"theme_{i}")])
                
                keyboard.append([InlineKeyboardButton("✏️ Написать свою тему", callback_data="custom_theme")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(themes_text, reply_markup=reply_markup, parse_mode='Markdown')
                
                # if usage:
                #     stats = (
                #         f"📊 **Статистика:**\n"
                #         f"• Входных токенов: {usage.get('input_tokens', 0)}\n"
                #         f"• Выходных токенов: {usage.get('output_tokens', 0)}\n"
                #         f"• Всего: {usage.get('total_tokens', 0)}"
                #     )
                #     await query.message.reply_text(stats, parse_mode='Markdown')
                    
            except Exception as e:
                logger.error(f"Ошибка генерации тем: {e}")
                await query.message.reply_text(f"❌ Ошибка: {e}\n\nИспользуйте /start")
        
        # Завершить
        elif data == "finish":
            await query.edit_message_text(
                "✅ Работа завершена!\n\n"
                "Используйте /start для новой сессии.",
                parse_mode='Markdown'
            )
            context.user_data.clear()

    async def text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        text = update.message.text.strip()
        
        # Обработка кнопки "Начать заново"
        if text == "🔄 Начать заново":
            context.user_data.clear()
            await self.start_command(update, context)
            return
        
        # Проверяем, выбрана ли техника
        if 'technique' not in context.user_data:
            await update.message.reply_text(
                "⚠️ Сначала выберите технику с помощью /start",
                reply_markup=self.main_keyboard
            )
            return
        
        # Проверяем, ждём ли мы пользовательскую тему
        if context.user_data.get('waiting_custom_theme'):
            theme_name = text
            context.user_data['current_theme'] = theme_name
            context.user_data['waiting_custom_theme'] = False
            
            # Запрашиваем длину поста (используем индекс вместо названия темы)
            keyboard = [
                [InlineKeyboardButton("📏 500 символов", callback_data="len_500")],
                [InlineKeyboardButton("📏 700 символов", callback_data="len_700")],
                [InlineKeyboardButton("📏 1000 символов", callback_data="len_1000")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ Тема: **{theme_name}**\n\nВыберите длину поста:",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "⚠️ Пожалуйста, используйте кнопки для выбора темы.\n\n"
                "Если хотите написать свою тему, нажмите кнопку '✏️ Написать свою тему'",
                reply_markup=self.main_keyboard
            )

    def parse_themes_list(self, themes_text: str) -> list:
        """Парсит список тем из текста (берет последние 10 если есть дубликаты)"""
        import re
        
        themes = []
        # Ищем темы с разными форматами нумерации
        patterns = [
            r'[1-9]️⃣\s+([^\[\n]+)',  # 1️⃣-9️⃣ (цифры с эмодзи)
            r'🔟\s+([^\[\n]+)',        # 🔟 (специальный эмодзи для "10")
            r'^\d+\)\s+([^\[\n]+)',            # 1) Тема
            r'^\d+\.\s+([^\[\n]+)',            # 1. Тема
        ]
        
        all_themes = []
        for pattern in patterns:
            matches = re.findall(pattern, themes_text, re.MULTILINE)
            if matches:
                for match in matches:
                    theme = match.strip()
                    # Удаляем текст в скобках и квадратных скобках в конце
                    theme = re.sub(r'\s*[\[\(].*$', '', theme).strip()
                    if theme:
                        all_themes.append(theme)
        
        logger.info(f"parse_themes_list: найдено {len(all_themes)} тем (до удаления дубликатов)")
        
        # Если нашли больше 10 тем (значит дубликаты), берем последние 10
        if len(all_themes) > 10:
            unique_themes = []
            for theme in reversed(all_themes):  # Идем с конца
                if theme not in unique_themes:
                    unique_themes.insert(0, theme)
                if len(unique_themes) == 10:
                    break
            logger.info(f"parse_themes_list: возвращаем {len(unique_themes)} уникальных тем")
            return unique_themes
        
        # Иначе убираем дубликаты сохраняя порядок
        seen = set()
        for theme in all_themes:
            if theme not in seen:
                seen.add(theme)
                themes.append(theme)
        
        logger.info(f"parse_themes_list: возвращаем {len(themes)} тем (после удаления дубликатов)")
        return themes[:10]  # Возвращаем максимум 10 тем

    async def generate_post_callback(self, query, theme_name: str, technique: str, post_length: int):
        """Генерирует пост и отправляет пользователю"""
        await query.edit_message_text(
            f"✍️ Генерирую пост на тему: **{theme_name}**\n"
            f"📊 Длина: {post_length} символов\n\n"
            f"⏳ Пожалуйста, подождите...",
            parse_mode='Markdown'
        )
        
        try:
            post, usage = self.natrium_bot.generate_post(
                theme=theme_name,
                technique=technique,
                post_length=post_length
            )
            
            # КРИТИЧЕСКОЕ ЛОГИРОВАНИЕ: проверяем что пришло от Яндекса
            logger.info(f"===== RAW POST FROM YANDEX (before processing) =====")
            logger.info(f"Length: {len(post)} chars")
            logger.info(f"First 200 chars: {post[:200]}")
            logger.info(f"Contains **: {('**' in post)}")
            if '**' in post:
                # Найдем все вхождения **
                import re
                bold_markers = re.findall(r'\*\*[^*]+\*\*', post)
                logger.info(f"Found {len(bold_markers)} bold markers: {bold_markers[:3]}")
            logger.info(f"=============================================\n")
            
            # КРИТИЧЕСКИ ВАЖНО: Полное удаление всех символов цитирования
            # Но НЕ трогаем содержимое поста!
            
            # КРИТИЧЕСКИ ВАЖНО: Удаляем шаги рассуждений модели
            # МЕТОД 1: Ищем маркер "ГОТОВЫЙ ПОСТ:"
            # МЕТОД 2: Ищем строку с эмодзи поста (💪🧠💤🔥⚡️💓🍽️) + ** + CAPS
            
            lines = post.split('\n')
            post_start_index = None
            
            # Эмодзи заголовков постов (НЕ путать с 🔄 🏋️ из рассуждений)
            post_emojis = ['💪', '🧠', '💤', '🔥', '⚡️', '💓', '🍽️', '🏃', '⚡', '📊', '🎯']
            
            # МЕТОД 1: Ищем маркер "ГОТОВЫЙ ПОСТ:"
            for i, line in enumerate(lines):
                if 'ГОТОВЫЙ ПОСТ:' in line.strip():
                    post_start_index = i + 1  # Начало после маркера
                    logger.info(f"Found 'ГОТОВЫЙ ПОСТ:' marker at line {i}")
                    break
            
            # МЕТОД 2: Если маркера нет, ищем по эмодзи + ** + CAPS
            if post_start_index is None:
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if not stripped or len(stripped) < 10:
                        continue
                    
                    # Пропускаем рассуждения: "ГЕНЕРИРУЮ", "Шаг", "FileSearch", "Web Search", JSON
                    if any(x in stripped for x in ['ГЕНЕРИРУЮ', 'Шаг', 'FileSearch', 'Web Search']):
                        continue
                    if stripped.startswith('{'):
                        continue
                    
                    # Ищем: эмодзи поста в начале + ** + CAPS
                    starts_with_post_emoji = any(stripped.startswith(emoji) for emoji in post_emojis)
                    if starts_with_post_emoji and '**' in stripped and any(c.isupper() for c in stripped):
                        post_start_index = i
                        logger.info(f"Found post start by emoji+CAPS pattern at line {i}")
                        break
            
            # Если нашли начало поста, берем только с этого момента
            if post_start_index is not None:
                removed_count = post_start_index
                lines = lines[post_start_index:]
                logger.info(f"Removed {removed_count} lines of reasoning steps")
            else:
                logger.warning("No reasoning steps detected, using full response")
            
            # Удаляем ТОЛЬКО начальные пустые строки и строки с >
            # Пропускаем пустые строки и строки с > В НАЧАЛЕ документа
            while lines and (not lines[0].strip() or lines[0].strip().startswith('>')):
                lines.pop(0)
            
            post = '\n'.join(lines).strip()
            
            # КРИТИЧЕСКИ ВАЖНО: Удаляем тройные обратные кавычки (```), которые конфликтуют с Telegram Markdown
            post = post.replace('```', '')
            logger.info(f"Removed ``` markers that conflict with Telegram Markdown")
            
            # КРИТИЧЕСКОЕ ЛОГИРОВАНИЕ: проверяем что осталось после очистки
            logger.info(f"===== POST AFTER CLEANING (before Telegram) =====")
            logger.info(f"Length: {len(post)} chars")
            logger.info(f"Contains **: {('**' in post)}")
            if '**' in post:
                import re
                bold_markers = re.findall(r'\*\*[^*]+\*\*', post)
                logger.info(f"Found {len(bold_markers)} bold markers after cleaning")
            logger.info(f"=============================================\n")
            
            # КРИТИЧЕСКАЯ ВАЛИДАЦИЯ: проверяем парность ** маркеров
            double_star_count = post.count('**')
            if double_star_count % 2 != 0:
                logger.error(f"⚠️ UNPAIRED ** markers detected! Count: {double_star_count}")
                logger.error(f"Text with unpaired markers:\n{post}")
                # Удаляем все ** если они непарные
                post = post.replace('**', '')
                logger.warning(f"Removed all ** markers to prevent Telegram parse error")
            
            # КРИТИЧЕСКИ ВАЖНО: Telegram Markdown использует * (один), а не ** (два)
            # Конвертируем ** в * для жирного текста
            post = post.replace('**', '*')
            logger.info(f"Converted ** to * for Telegram Markdown")
            
            # Нормализация источников: WHO → ВОЗ для единообразия
            post = post.replace('WHO', 'ВОЗ')
            post = post.replace('(WHO)', '(ВОЗ)')
            logger.info(f"Normalized WHO → ВОЗ for consistency")
            
            # Автоматически оборачиваем источники в круглые скобки, если они без скобок
            import re
            
            # СНАЧАЛА обрабатываем crossfit.com (чтобы не превратить в (CrossFit).com)
            post = re.sub(r'\s+crossfit\.com\.', r' (crossfit.com).', post, flags=re.IGNORECASE)
            post = re.sub(r'\s+crossfit\.com\n', r' (crossfit.com)\n', post, flags=re.IGNORECASE)
            post = re.sub(r'\s+crossfit\.com$', r' (crossfit.com)', post, flags=re.IGNORECASE)
            
            # ПОТОМ обрабатываем остальные источники (но НЕ CrossFit без .com)
            # CrossFit часто используется как часть текста ("в CrossFit", "для CrossFit атлетов")
            # Поэтому НЕ оборачиваем его автоматически - модель сама должна ставить скобки
            # Паттерн: источник в конце строки без скобок (ВОЗ, PubMed, Исследования)
            sources = ['ВОЗ', 'PubMed', 'Исследования', 'Исследование']
            for source in sources:
                # Заменяем источник в конце предложения без скобок на вариант со скобками
                # Пример: "текст ВОЗ." → "текст (ВОЗ)."
                post = re.sub(rf'\s+{source}\.', f' ({source}).', post)
                post = re.sub(rf'\s+{source}\n', f' ({source})\n', post)
                post = re.sub(rf'\s+{source}$', f' ({source})', post)
            
            logger.info(f"Wrapped sources in parentheses (ВОЗ, PubMed, Исследования, crossfit.com)")
            
            # ФИНАЛЬНОЕ ЛОГИРОВАНИЕ перед отправкой в Telegram
            logger.info(f"===== FINAL TEXT SENT TO TELEGRAM =====")
            logger.info(f"Length: {len(post)} chars")
            logger.info(f"Single * count: {post.count('*')}")
            logger.info(f"First 200 chars: {post[:200]}")
            logger.info(f"Last 200 chars: {post[-200:]}")
            logger.info(f"=====================================\n")
            
            # Отправляем пост БЕЗ заголовка (для прямого копирования в канал)
            await query.message.reply_text(
                post,
                parse_mode='Markdown'
            )
            
            # # Статистика (закомментировано)
            # if usage:
            #     stats = (
            #         f"📊 **Статистика:**\n"
            #         f"• Входных токенов: {usage.get('input_tokens', 0)}\n"
            #         f"• Выходных токенов: {usage.get('output_tokens', 0)}\n"
            #         f"• Всего: {usage.get('total_tokens', 0)}"
            #     )
            #     await query.message.reply_text(stats, parse_mode='Markdown')
            
            # Меню действий (используем короткие callback без темы)
            keyboard = [
                [InlineKeyboardButton("🔄 Новый пост на эту тему", callback_data="regen")],
                [InlineKeyboardButton("📋 Другая тема", callback_data="other_theme")],
                [InlineKeyboardButton("🆕 Новые темы", callback_data="new_themes")],
                [InlineKeyboardButton("🏁 Завершить", callback_data="finish")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.reply_text(
                "🎯 **Что делать дальше?**",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
        except Exception as e:
            logger.error(f"Ошибка генерации поста: {e}")
            await query.message.reply_text(
                f"❌ Ошибка при генерации поста: {e}\n\n"
                "Попробуйте ещё раз или используйте /start"
            )

    def run(self):
        """Запуск бота"""
        logger.info("🚀 Telegram-бот запущен!")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    # Проверяем блокировку перед запуском
    if not acquire_lock():
        sys.exit(1)
    
    try:
        bot = TelegramSMMBot()
        bot.run()
    except KeyboardInterrupt:
        logger.info("⚠️ Получен сигнал остановки (Ctrl+C)")
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота: {e}")
    finally:
        release_lock()
