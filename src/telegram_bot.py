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

# Глобальные настройки и счетчики (для каждого пользователя)
# ВАЖНО: USER_SETTINGS хранится в RAM и сбрасывается при перезапуске бота
USER_SETTINGS = {}  # {user_id: {'show_token_stats': False}}  # по умолчанию выключено
USER_SESSION_STATS = {}  # {user_id: {...}}

# Тарифы Yandex Cloud GPT (руб. за 1000 токенов)
PRICING = {
    'input': 0.0012,
    'output': 0.0012,
    'cached': 0.0006
}


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
        
        # Пытаемся найти PID другого процесса
        other_pid = None
        try:
            # Пробуем прочитать PID файл (может не получиться если файл заблокирован)
            if PID_FILE.exists():
                with open(PID_FILE, 'r') as f:
                    other_pid = f.read().strip()
        except:
            pass
        
        # Если не удалось прочитать файл, ищем процесс через ps
        if not other_pid:
            try:
                import subprocess
                result = subprocess.run(
                    ['pgrep', '-f', 'telegram_bot.py'],
                    capture_output=True,
                    text=True
                )
                pids = result.stdout.strip().split('\n')
                if pids and pids[0]:
                    other_pid = pids[0]
            except:
                pass
        
        # Выводим сообщение
        if other_pid:
            logger.error(f"❌ ОШИБКА: Другой экземпляр бота уже запущен (PID: {other_pid})")
            print(f"\n❌ ОШИБКА: Другой экземпляр natrium-smm-bot уже запущен!")
            print(f"   PID запущенного процесса: {other_pid}")
            print(f"\nЧтобы остановить его, выполните:")
            print(f"   sudo systemctl stop natrium-smm-bot")
            print(f"   или: kill {other_pid}\n")
        else:
            logger.error("❌ ОШИБКА: Другой экземпляр бота уже запущен")
            print(f"\n❌ ОШИБКА: Другой экземпляр natrium-smm-bot уже запущен!")
            print(f"   Используйте команду для остановки:")
            print(f"   sudo systemctl stop natrium-smm-bot\n")
        
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


def convert_markdown_to_html(text: str) -> str:
    """
    Конвертирует Markdown форматирование в HTML для Telegram
    
    Поддерживаемые преобразования:
    - [текст](URL) → <a href="URL">текст</a>
    - **текст** → <b>текст</b>
    - *текст* → <i>текст</i>
    
    Args:
        text: Текст с Markdown форматированием
        
    Returns:
        Текст с HTML форматированием
    """
    import re
    
    # 1. Конвертируем ссылки: [текст](URL) → <a href="URL">текст</a>
    # Паттерн для поиска Markdown ссылок
    text = re.sub(
        r'\[([^\]]+)\]\(([^)]+)\)',
        r'<a href="\2">\1</a>',
        text
    )
    
    # 2. Конвертируем жирный текст: **текст** → <b>текст</b>
    # Важно: делать это ПОСЛЕ конвертации ссылок, чтобы не сломать паттерны
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    
    # 3. Конвертируем курсив: *текст* → <i>текст</i>
    # Но НЕ трогаем одинарные * в начале строки (буллеты)
    # Паттерн: * не в начале строки, окруженный текстом с обеих сторон
    text = re.sub(r'(?<!^)(?<!\n)\*([^*\n]+?)\*', r'<i>\1</i>', text, flags=re.MULTILINE)
    
    return text


def get_user_settings(user_id: int) -> dict:
    """Получить настройки пользователя (по умолчанию статистика выключена)"""
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = {'show_token_stats': False}
    return USER_SETTINGS[user_id]


def get_user_stats(user_id: int) -> dict:
    """Получить статистику сессии пользователя"""
    if user_id not in USER_SESSION_STATS:
        USER_SESSION_STATS[user_id] = {
            'total_input_tokens': 0,
            'total_output_tokens': 0,
            'total_cached_tokens': 0,
            'total_reasoning_tokens': 0,
            'total_requests': 0,
            'total_tokens': 0
        }
    return USER_SESSION_STATS[user_id]


def format_token_stats(operation: str, usage: dict, user_id: int) -> str:
    """Форматирует статистику токенов для отправки в Telegram (HTML формат)"""
    if not usage:
        return ""

    input_tokens = usage.get('input_tokens', 0)
    output_tokens = usage.get('output_tokens', 0)
    total_tokens = usage.get('total_tokens', 0)

    # Получаем детали
    input_details = usage.get('input_tokens_details')
    output_details = usage.get('output_tokens_details')

    cached_tokens = 0
    if input_details:
        cached_tokens = getattr(input_details, 'cached_tokens', 0) if hasattr(input_details, 'cached_tokens') else input_details.get('cached_tokens', 0)

    reasoning_tokens = 0
    if output_details:
        reasoning_tokens = getattr(output_details, 'reasoning_tokens', 0) if hasattr(output_details, 'reasoning_tokens') else output_details.get('reasoning_tokens', 0)

    # Обновляем накопительную статистику
    stats = get_user_stats(user_id)
    stats['total_input_tokens'] += input_tokens
    stats['total_output_tokens'] += output_tokens
    stats['total_cached_tokens'] += cached_tokens
    stats['total_reasoning_tokens'] += reasoning_tokens
    stats['total_requests'] += 1
    stats['total_tokens'] += total_tokens

    # Расчет стоимости текущего запроса
    cost_input = (input_tokens - cached_tokens) / 1000 * PRICING['input']
    cost_cached = cached_tokens / 1000 * PRICING['cached']
    cost_output = output_tokens / 1000 * PRICING['output']
    total_cost = cost_input + cost_cached + cost_output

    # Формируем текст статистики в HTML формате
    text = f"📊 <b>{operation}</b>\n"
    text += f"\n🔢 <b>Токены текущего запроса:</b>\n"
    text += f"   • Входные: {input_tokens}\n"
    if cached_tokens > 0:
        cache_percent = (cached_tokens / input_tokens * 100) if input_tokens > 0 else 0
        text += f"      └ из кеша: {cached_tokens} ({cache_percent:.1f}% 💾)\n"
    text += f"   • Выходные: {output_tokens}\n"
    if reasoning_tokens > 0:
        text += f"      └ reasoning: {reasoning_tokens}\n"
    text += f"   • Всего: {total_tokens}\n"

    # Соотношение input/output
    if output_tokens > 0:
        ratio = input_tokens / output_tokens
        text += f"\n📈 <b>Соотношение in/out:</b> {ratio:.2f}:1"
        if ratio > 5:
            text += " (много контекста)\n"
        elif ratio < 1:
            text += " (длинная генерация)\n"
        else:
            text += " (оптимально)\n"

    # Стоимость
    text += f"\n💰 <b>Стоимость запроса:</b> ~{total_cost:.4f} ₽"
    if cached_tokens > 0:
        saved = (cached_tokens / 1000 * (PRICING['input'] - PRICING['cached']))
        text += f" (экономия: {saved:.4f} ₽)\n"
    else:
        text += "\n"

    # Накопительная статистика
    total_session_cost = (
        (stats['total_input_tokens'] - stats['total_cached_tokens']) / 1000 * PRICING['input'] +
        stats['total_cached_tokens'] / 1000 * PRICING['cached'] +
        stats['total_output_tokens'] / 1000 * PRICING['output']
    )

    text += f"\n📦 <b>Статистика сессии</b> (запросов: {stats['total_requests']}): \n"
    text += f"   • Всего токенов: {stats['total_tokens']}\n"
    text += f"   • Входные: {stats['total_input_tokens']}\n"
    if stats['total_cached_tokens'] > 0:
        cache_percent_total = (stats['total_cached_tokens'] / stats['total_input_tokens'] * 100) if stats['total_input_tokens'] > 0 else 0
        text += f"      └ из кеша: {stats['total_cached_tokens']} ({cache_percent_total:.1f}% 💾)\n"
    text += f"   • Выходные: {stats['total_output_tokens']}\n"
    text += f"   • Стоимость: ~{total_session_cost:.4f} ₽\n"

    return text


class TelegramSMMBot:
    def __init__(self):
        if not TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN не найден в переменных окружения. Проверьте GitHub Secrets.")
        
        self.natrium_bot = NatriumBot()
        self.application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Постоянная клавиатура с кнопками
        self.main_keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("🔄 Начать заново")],
                [KeyboardButton("⚙️ Настройки")]
            ],
            resize_keyboard=True,
            one_time_keyboard=False
        )
        
        # Регистрация обработчиков
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("update_prompt", self.update_prompt_command))
        self.application.add_handler(CallbackQueryHandler(self.button_handler))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_handler))

    async def update_prompt_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обновляет системный промпт агента в Yandex Cloud (только для администраторов)"""
        user_id = update.effective_user.id
        
        # Проверка прав администратора (укажите свой Telegram ID)
        ADMIN_IDS = [int(os.getenv("ADMIN_TELEGRAM_ID", "0"))]  # Добавьте свой ID в .env
        
        if user_id not in ADMIN_IDS and ADMIN_IDS != [0]:
            await update.message.reply_text("❌ Эта команда доступна только администраторам.")
            return
        
        await update.message.reply_text(
            "🔄 <b>Обновление системного промпта агента...</b>\n\n"
            "⏳ Это может занять несколько секунд.",
            parse_mode='HTML'
        )
        
        try:
            success = self.natrium_bot.update_agent_prompt()
            
            if success:
                await update.message.reply_text(
                    "✅ <b>Системный промпт успешно обновлён!</b>\n\n"
                    "Агент Yandex Cloud теперь использует новые требования:\n"
                    "• Обязательный заголовок в CAPS\n"
                    "• Обязательный лид-затравка после заголовка\n"
                    "• Усиленный контроль структуры постов",
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    "❌ <b>Ошибка обновления промпта</b>\n\n"
                    "Проверьте логи для деталей.",
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Error in update_prompt_command: {e}")
            await update.message.reply_text(
                f"❌ <b>Ошибка:</b> {str(e)}",
                parse_mode='HTML'
            )

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        
        # Устанавливаем оптимальную технику cov+cok
        technique = 'cov+cok'
        context.user_data['technique'] = technique
        
        welcome_text = f"""
🤖 <b>Привет, {user.first_name}!</b>

Я бот для генерации контента для Натриум Фитнесс.

🎯 <b>Мои возможности:</b>
• Генерация актуальных тем для постов
• Создание готовых постов с эмодзи и хештегами
• Проверенные факты из научных источников

📚 <b>База знаний:</b>
• CrossFit методики
• Исследования ВОЗ и PubMed
• Книга о соцсетях
"""
        
        await update.message.reply_text(welcome_text, parse_mode='HTML', reply_markup=self.main_keyboard)
        
        # Показываем выбор фокуса вместо сразу генерации тем
        focus_text = "🎯 <b>НА ЧТО СДЕЛАТЬ УПОР В ТЕМАХ?</b>\n\nВыберите направление:"
        
        keyboard = [
            [InlineKeyboardButton("🍽️ Питание и диета", callback_data="focus_nutrition")],
            [InlineKeyboardButton("💪 Спорт и тренировки (CrossFit, силовые)", callback_data="focus_sport")],
            [InlineKeyboardButton("💤 Сон и восстановление", callback_data="focus_sleep")],
            [InlineKeyboardButton("🤸 Техника упражнений (гимнастика, атлетика)", callback_data="focus_technique")],
            [InlineKeyboardButton("🏥 Здоровье и профилактика (ВОЗ)", callback_data="focus_health")],
            [InlineKeyboardButton("🎲 Разное (без фокуса)", callback_data="focus_random")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(focus_text, reply_markup=reply_markup, parse_mode='HTML')

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
                    f"✅ Тема: <b>{theme_name}</b>\n\nВыберите длину поста:",
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
        
        # Пользователь хочет написать свою тему
        elif data == "custom_theme":
            await query.edit_message_text(
                "✏️ Напишите свою тему для поста:",
                parse_mode='HTML'
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
                    parse_mode='HTML'
                )
                return
            
            await self.generate_post_callback(query, theme_name, technique, post_length)
        
        # Регенерация поста (используем current_theme из контекста)
        elif data == "regen":
            theme_name = context.user_data.get('current_theme', '')
            
            if not theme_name:
                await query.edit_message_text(
                    "❌ Ошибка: тема не найдена. Используйте /start",
                    parse_mode='HTML'
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
                f"✅ Тема: <b>{theme_name}</b>\n\nВыберите длину поста:",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        
        # Другая тема - показываем УЖЕ сгенерированные темы
        elif data == "other_theme":
            parsed_themes = context.user_data.get('parsed_themes', [])
            
            if not parsed_themes:
                await query.edit_message_text(
                    "❌ Темы не найдены. Используйте /start",
                    parse_mode='HTML'
                )
                return
            
            # Формируем сообщение БЕЗ перечисления тем
            bulb = chr(0x1F4A1)  # 💡
            themes_text = (
                f"{bulb} Выберите тему:\n\n"
                f"<i>Длинная тема → 🔄📱</i>"
            )
            
            # Создаём кнопки для ВСЕХ найденных тем
            keyboard = []
            for i, theme in enumerate(parsed_themes, 1):
                # Нормализуем регистр: первая буква заглавная
                normalized_theme = theme.capitalize()
                # Добавляем номер темы перед текстом
                button_text = f"{i}. {normalized_theme}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"theme_{i}")])
            
            # Кнопка "Другие темы по этому направлению" перед "Написать свою тему"
            keyboard.append([InlineKeyboardButton("🔄 Другие темы по этому направлению", callback_data="regenerate_same_focus")])
            keyboard.append([InlineKeyboardButton("✏️ Написать свою тему", callback_data="custom_theme")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(themes_text, reply_markup=reply_markup, parse_mode='HTML')
        
        # Новые темы - показываем выбор фокуса
        elif data == "new_themes":
            # Показываем кнопки выбора фокуса
            focus_text = "🎯 <b>НА ЧТО СДЕЛАТЬ УПОР В НОВЫХ ТЕМАХ?</b>\n\nВыберите направление:"
            
            keyboard = [
                [InlineKeyboardButton("🍽️ Питание и диета", callback_data="focus_nutrition")],
                [InlineKeyboardButton("💪 Спорт и тренировки (CrossFit, силовые)", callback_data="focus_sport")],
                [InlineKeyboardButton("💤 Сон и восстановление", callback_data="focus_sleep")],
                [InlineKeyboardButton("🤸 Техника упражнений (гимнастика, атлетика)", callback_data="focus_technique")],
                [InlineKeyboardButton("🏥 Здоровье и профилактика (ВОЗ)", callback_data="focus_health")],
                [InlineKeyboardButton("🎲 Разное (без фокуса)", callback_data="focus_random")]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(focus_text, reply_markup=reply_markup, parse_mode='HTML')
        
        # Регенерация тем с тем же фокусом
        elif data == "regenerate_same_focus":
            # Получаем последний фокус из контекста
            last_focus = context.user_data.get('last_focus', 'random')
            logger.info(f"Regenerating themes with focus: {last_focus}")
            
            # Имитируем выбор того же фокуса
            query.data = f"focus_{last_focus}"
            # Продолжаем обработку как focus_
        
        # Обработка выбора фокуса для новых тем
        elif data.startswith("focus_"):
            focus_type = data.replace("focus_", "")
            
            # Сохраняем последний фокус в контексте для кнопки "Другие темы"
            context.user_data['last_focus'] = focus_type
            
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
                parse_mode='HTML'
            )
            
            try:
                # Получаем предыдущие темы для избежания повторений
                all_previous_themes = context.user_data.get('all_generated_themes', [])
                
                # Формируем custom_input с фокусом
                if focus_keywords:
                    custom_input = f"Сгенерируй 10 актуальных тем для постов с ФОКУСОМ НА: {focus_keywords}. Обязательно используй разнообразные форматы из книги о соцсетях!"
                else:
                    custom_input = None
                
                # Передаём предыдущие темы для избежания повторений
                themes, usage = self.natrium_bot.generate_themes(
                    technique, 
                    custom_input=custom_input,
                    previous_themes=all_previous_themes
                )
                context.user_data['themes'] = themes
                
                # Парсим темы и создаём кнопки
                parsed_themes = self.parse_themes_list(themes)
                context.user_data['parsed_themes'] = parsed_themes
                
                # Добавляем новые темы к списку всех сгенерированных тем
                all_previous_themes.extend(parsed_themes)
                context.user_data['all_generated_themes'] = all_previous_themes
                logger.info(f"focus_{focus_type}: всего сгенерировано тем в сессии: {len(all_previous_themes)}")
                
                logger.info(f"focus_{focus_type}: распарсено {len(parsed_themes)} тем для кнопок")
                
                # Формируем сообщение БЕЗ перечисления тем
                bulb = chr(0x1F4A1)  # 💡
                themes_text = (
                    f"{bulb} Выберите тему:\n\n"
                    f"<i>Длинная тема → 🔄📱</i>"
                )
                
                # Создаём кнопки для ВСЕХ найденных тем
                keyboard = []
                for i, theme in enumerate(parsed_themes, 1):
                    # Нормализуем регистр: первая буква заглавная
                    normalized_theme = theme.capitalize()
                    # Добавляем номер темы перед текстом
                    button_text = f"{i}. {normalized_theme}"
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=f"theme_{i}")])
                
                # Кнопка "Другие темы по этому направлению" перед "Написать свою тему"
                keyboard.append([InlineKeyboardButton("🔄 Другие темы по этому направлению", callback_data=f"focus_{focus_type}")])
                keyboard.append([InlineKeyboardButton("✏️ Написать свою тему", callback_data="custom_theme")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(themes_text, reply_markup=reply_markup, parse_mode='HTML')
                
                # Отправляем статистику, если включена
                if usage:
                    user_id = query.from_user.id
                    settings = get_user_settings(user_id)
                    if settings['show_token_stats']:
                        stats_text = format_token_stats("Генерация тем", usage, user_id)
                        await query.message.reply_text(stats_text, parse_mode='HTML')
                    
            except Exception as e:
                logger.error(f"Ошибка генерации тем: {e}")
                await query.message.reply_text(f"❌ Ошибка: {e}\n\nИспользуйте /start")
        
        # Завершить
        elif data == "finish":
            await query.edit_message_text(
                "✅ Работа завершена!\n\n"
                "Используйте /start для новой сессии.",
                parse_mode='HTML'
            )
            context.user_data.clear()
        
        # Настройки
        elif data == "settings":
            await self.show_settings_menu(query, context)
        
        # Переключение вывода статистики токенов
        elif data == "toggle_stats":
            user_id = query.from_user.id
            settings = get_user_settings(user_id)
            settings['show_token_stats'] = not settings['show_token_stats']
            await self.show_settings_menu(query, context)
        
        # Сброс счетчиков сессии
        elif data == "reset_stats":
            user_id = query.from_user.id
            USER_SESSION_STATS[user_id] = {
                'total_input_tokens': 0,
                'total_output_tokens': 0,
                'total_cached_tokens': 0,
                'total_reasoning_tokens': 0,
                'total_requests': 0,
                'total_tokens': 0
            }
            await query.answer("✅ Счетчики сессии сброшены", show_alert=True)
            await self.show_settings_menu(query, context)
        
        # Показать текущую статистику сессии
        elif data == "view_stats":
            user_id = query.from_user.id
            stats = get_user_stats(user_id)
            
            if stats['total_requests'] == 0:
                await query.answer("⚠️ Запросов ещё не было", show_alert=True)
            else:
                total_cost = (
                    (stats['total_input_tokens'] - stats['total_cached_tokens']) / 1000 * PRICING['input'] +
                    stats['total_cached_tokens'] / 1000 * PRICING['cached'] +
                    stats['total_output_tokens'] / 1000 * PRICING['output']
                )
                cache_percent = (stats['total_cached_tokens'] / stats['total_input_tokens'] * 100) if stats['total_input_tokens'] > 0 else 0
                avg_tokens = stats['total_tokens'] / stats['total_requests']
                
                stats_text = f"📊 <b>СТАТИСТИКА СЕССИИ</b>\n\n"
                stats_text += f"📦 <b>Запросов:</b> {stats['total_requests']}\n\n"
                stats_text += f"🔢 <b>Токены:</b>\n"
                stats_text += f"   • Всего: {stats['total_tokens']}\n"
                stats_text += f"   • Входные: {stats['total_input_tokens']}\n"
                stats_text += f"      └ из кеша: {stats['total_cached_tokens']} ({cache_percent:.1f}% 💾)\n"
                stats_text += f"   • Выходные: {stats['total_output_tokens']}\n"
                if stats['total_reasoning_tokens'] > 0:
                    stats_text += f"      └ reasoning: {stats['total_reasoning_tokens']}\n"
                stats_text += f"   • Средне/запрос: {avg_tokens:.0f}\n\n"
                stats_text += f"💰 <b>Общая стоимость:</b> ~{total_cost:.4f} ₽\n"
                if stats['total_cached_tokens'] > 0:
                    saved = (stats['total_cached_tokens'] / 1000 * (PRICING['input'] - PRICING['cached']))
                    stats_text += f"   └ Экономия на кеше: ~{saved:.4f} ₽"
                
                await query.answer()
                await query.message.reply_text(stats_text, parse_mode='HTML')
        
        # Закрыть настройки
        elif data == "close_settings":
            await query.edit_message_text(
                "⚙️ Настройки закрыты.\n\n"
                "Используйте кнопку <b>⚙️ Настройки</b> для повторного открытия.",
                parse_mode='HTML'
            )

    async def text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик текстовых сообщений"""
        text = update.message.text.strip()
        
        # Обработка кнопки "Начать заново"
        if text == "🔄 Начать заново":
            context.user_data.clear()
            await self.start_command(update, context)
            return
        
        # Обработка кнопки "Настройки"
        if text == "⚙️ Настройки":
            await self.show_settings_menu_message(update, context)
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
                f"✅ Тема: <b>{theme_name}</b>\n\nВыберите длину поста:",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                "⚠️ Пожалуйста, используйте кнопки для выбора темы.\n\n"
                "Если хотите написать свою тему, нажмите кнопку '✏️ Написать свою тему'",
                reply_markup=self.main_keyboard
            )

    async def show_settings_menu(self, query, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню настроек (callback version)"""
        user_id = query.from_user.id
        settings = get_user_settings(user_id)
        
        status = "✅ Включен" if settings['show_token_stats'] else "❌ Выключен"
        
        text = f"⚙️ <b>НАСТРОЙКИ БОТА</b>\n\n"
        text += f"📊 <b>Вывод статистики токенов:</b> {status}\n"
        
        keyboard = [
            [InlineKeyboardButton(
                "🔄 Переключить статистику" if settings['show_token_stats'] else "✅ Включить статистику",
                callback_data="toggle_stats"
            )],
            [InlineKeyboardButton("🔄 Сбросить счетчики сессии", callback_data="reset_stats")],
            [InlineKeyboardButton("📊 Показать статистику сессии", callback_data="view_stats")],
            [InlineKeyboardButton("✖️ Закрыть", callback_data="close_settings")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
    
    async def show_settings_menu_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню настроек (message version)"""
        user_id = update.effective_user.id
        settings = get_user_settings(user_id)
        
        status = "✅ Включен" if settings['show_token_stats'] else "❌ Выключен"
        
        text = f"⚙️ <b>НАСТРОЙКИ БОТА</b>\n\n"
        text += f"📊 <b>Вывод статистики токенов:</b> {status}\n"
        
        keyboard = [
            [InlineKeyboardButton(
                "🔄 Переключить статистику" if settings['show_token_stats'] else "✅ Включить статистику",
                callback_data="toggle_stats"
            )],
            [InlineKeyboardButton("🔄 Сбросить счетчики сессии", callback_data="reset_stats")],
            [InlineKeyboardButton("📊 Показать статистику сессии", callback_data="view_stats")],
            [InlineKeyboardButton("✖️ Закрыть", callback_data="close_settings")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='HTML')
    
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
            f"✍️ Генерирую пост на тему: <b>{theme_name}</b>\n"
            f"📊 Длина: {post_length} символов\n\n"
            f"⏳ Пожалуйста, подождите...",
            parse_mode='HTML'
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
            # МЕТОД 3: Удаляем строки с артефактами рассуждений
            
            lines = post.split('\n')
            post_start_index = None
            
            # Эмодзи заголовков постов (НЕ путать с 🔄 🏋️ из рассуждений)
            post_emojis = ['💪', '🧠', '💤', '🔥', '⚡️', '💓', '🍽️', '🏃', '⚡', '📊', '🎯']
            
            # Артефакты рассуждений (удаляем эти строки ПОЛНОСТЬЮ)
            reasoning_markers = [
                '🔄 Сначала мне нужно',
                '[Вызов функции',
                'search_index',
                'web_search',
                'FileSearch',
                'Web Search',
                'ГЕНЕРИРУЮ',
                'Шаг 1',
                'Шаг 2',
                'Шаг 3',
                'для поиска',
                'с запросом'
            ]
            
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
                    
                    # Пропускаем рассуждения
                    if any(marker in stripped for marker in reasoning_markers):
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
            
            # КРИТИЧЕСКИ ВАЖНО: Удаляем тройные обратные кавычки (```), которые конфликтуют с форматированием
            post = post.replace('```', '')
            logger.info(f"Removed ``` markers")
            
            # КРИТИЧЕСКОЕ ЛОГИРОВАНИЕ: проверяем что осталось после очистки
            logger.info(f"===== POST AFTER CLEANING (before HTML conversion) =====")
            logger.info(f"Length: {len(post)} chars")
            logger.info(f"Contains **: {('**' in post)}")
            logger.info(f"Contains [link]: {('[' in post and '](' in post)}")
            if '**' in post:
                import re
                bold_markers = re.findall(r'\*\*[^*]+\*\*', post)
                logger.info(f"Found {len(bold_markers)} bold markers after cleaning")
            logger.info(f"=============================================\n")
            
            # НЕ проверяем парность ** - это сделает convert_markdown_to_html()
            # Удаляем старую валидацию для Markdown
            
            # Нормализация источников: WHO → ВОЗ для единообразия
            post = post.replace('WHO', 'ВОЗ')
            post = post.replace('(WHO)', '(ВОЗ)')
            logger.info(f"Normalized WHO → ВОЗ for consistency")
            
            # КРИТИЧЕСКИ ВАЖНО: Конвертируем Markdown в HTML ДО обработки источников
            # Это сохранит правильные ссылки [PubMed](URL) → <a href="URL">PubMed</a>
            # Яндекс генерирует ссылки в формате [текст](URL)
            # Telegram с parse_mode='HTML' требует <a href="URL">текст</a>
            post = convert_markdown_to_html(post)
            logger.info(f"Converted Markdown to HTML (links preserved)")
            
            # ПОСЛЕ конвертации в HTML обрабатываем источники
            # Теперь ссылки в формате <a href="URL">PubMed</a> и мы их НЕ трогаем
            import re
            
            # СНАЧАЛА обрабатываем crossfit.com (чтобы не превратить в (CrossFit).com)
            # Обрабатываем разные случаи: с точкой, точкой с запятой, переносом строки, в конце
            # НО НЕ трогаем если это внутри HTML тега <a>
            # Паттерн: crossfit.com НЕ внутри <a>...</a>
            post = re.sub(r'(?<!>)\s+crossfit\.com([\.;,!\?])', r' (crossfit.com)\1', post, flags=re.IGNORECASE)
            post = re.sub(r'(?<!>)\s+crossfit\.com\n', r' (crossfit.com)\n', post, flags=re.IGNORECASE)
            post = re.sub(r'(?<!>)\s+crossfit\.com$', r' (crossfit.com)', post, flags=re.IGNORECASE)
            
            # ПОТОМ обрабатываем остальные источники (но НЕ CrossFit без .com)
            # Расширенная обработка: точка, точка с запятой, запятая, восклицательный знак, вопросительный знак
            # ВАЖНО: НЕ трогаем источники внутри HTML тегов <a>источник</a>
            sources = ['ВОЗ', 'PubMed', 'Исследования', 'Исследование']
            for source in sources:
                # Заменяем источник с разными знаками препинания ТОЛЬКО если он НЕ внутри <a>...</a>
                # Negative lookbehind (?<!>) - НЕ после >
                # Пример: "текст ВОЗ." → "текст (ВОЗ).", "текст PubMed;" → "текст (PubMed);"
                # НО: "<a href='...'>PubMed</a>" остаётся без изменений
                post = re.sub(rf'(?<!>)\s+{source}([\.;,!\?])', f' ({source})\\1', post)
                post = re.sub(rf'(?<!>)\s+{source}\n', f' ({source})\n', post)
                post = re.sub(rf'(?<!>)\s+{source}$', f' ({source})', post)
            
            logger.info(f"Wrapped sources in parentheses (ВОЗ, PubMed, Исследования, crossfit.com)")
            
            # ДОПОЛНИТЕЛЬНО: Если остались артефакты типа (PubMed)(URL) - исправляем их
            # Это происходит если Яндекс сгенерировал (Source)(URL) вместо [Source](URL)
            # Конвертируем (Source)(URL) → <a href="URL">Source</a>
            for source in sources + ['crossfit.com', 'ВОЗ']:
                # Паттерн: (Источник)(http...)
                pattern = rf'\({re.escape(source)}\)\((https?://[^\)]+)\)'
                replacement = f'<a href="\\1">{source}</a>'
                post = re.sub(pattern, replacement, post, flags=re.IGNORECASE)
            
            logger.info(f"Fixed malformed links (Source)(URL) → <a href>Source</a>")
            
            # КРИТИЧЕСКИ ВАЖНО: Удаляем артефакты рассуждений модели после хештегов
            # Ищем последнюю строку с хештегами (начинается с #)
            lines = post.split('\n')
            last_hashtag_index = -1
            for i in range(len(lines) - 1, -1, -1):
                stripped = lines[i].strip()
                if stripped and stripped.startswith('#'):
                    last_hashtag_index = i
                    break
            
            # Если нашли хештеги, обрезаем все что после них
            if last_hashtag_index >= 0:
                # Берем только строки до хештегов включительно
                post = '\n'.join(lines[:last_hashtag_index + 1])
                logger.info(f"Removed reasoning artifacts after hashtags (line {last_hashtag_index})")
            
            # HTML конвертация уже выполнена ВЫШЕ (до обработки источников)
            # Это важно для сохранения правильных ссылок [text](URL) → <a href="URL">text</a>
            
            # ФИНАЛЬНОЕ ЛОГИРОВАНИЕ перед отправкой в Telegram
            logger.info(f"===== FINAL TEXT SENT TO TELEGRAM =====")
            logger.info(f"Length: {len(post)} chars")
            logger.info(f"Contains <a href: {('<a href' in post)}")
            logger.info(f"Contains <b>: {('<b>' in post)}")
            logger.info(f"First 200 chars: {post[:200]}")
            logger.info(f"Last 200 chars: {post[-200:]}")
            logger.info(f"=====================================\n")
            
            # Отправляем пост БЕЗ заголовка (для прямого копирования в канал)
            await query.message.reply_text(
                post,
                parse_mode='HTML'
            )
            
            # Отправляем статистику, если включена
            if usage:
                # Получаем user_id из context (query.from_user может быть недоступен)
                user_id = query.from_user.id
                settings = get_user_settings(user_id)
                if settings['show_token_stats']:
                    stats_text = format_token_stats("Генерация поста", usage, user_id)
                    await query.message.reply_text(stats_text, parse_mode='HTML')
            
            # Меню действий (используем короткие callback без темы)
            keyboard = [
                [InlineKeyboardButton("🔄 Новый пост на эту тему", callback_data="regen")],
                [InlineKeyboardButton("📋 Другая тема", callback_data="other_theme")],
                [InlineKeyboardButton("🆕 Новые темы", callback_data="new_themes")],
                [InlineKeyboardButton("🏁 Завершить", callback_data="finish")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.reply_text(
                "🎯 <b>Что делать дальше?</b>",
                reply_markup=reply_markup,
                parse_mode='HTML'
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
