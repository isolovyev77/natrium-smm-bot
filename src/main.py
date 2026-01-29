import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.bot import NatriumBot


def clear_screen():
    """Очищает экран (опционально)"""
    # os.system('clear' if os.name == 'posix' else 'cls')
    pass


def print_separator():
    """Печатает разделитель"""
    print("\n" + "="*70 + "\n")


def print_token_usage(operation: str, usage: dict):
    """Печатает статистику использования токенов"""
    if not usage:
        return

    input_tokens = usage.get('input_tokens', 0)
    output_tokens = usage.get('output_tokens', 0)
    total_tokens = usage.get('total_tokens', 0)

    print("\n" + "-"*70)
    print(f"📊 {operation}")
    print(f"Tokens in/out/total: {input_tokens}+{output_tokens}={total_tokens}")

    # Выводим дополнительные детали если есть
    input_details = usage.get('input_tokens_details')
    output_details = usage.get('output_tokens_details')

    if input_details:
        cached = getattr(input_details, 'cached_tokens', 0) if hasattr(input_details, 'cached_tokens') else input_details.get('cached_tokens', 0)
        if cached > 0:
            print(f"   └─ cached: {cached} токенов")

    if output_details:
        reasoning = getattr(output_details, 'reasoning_tokens', 0) if hasattr(output_details, 'reasoning_tokens') else output_details.get('reasoning_tokens', 0)
        if reasoning > 0:
            print(f"   └─ reasoning: {reasoning} токенов")

    print("-"*70 + "\n")


def get_technique_choice():
    """Запрашивает выбор техники промптинга"""
    print("🎯 ВЫБЕРИТЕ ТЕХНИКУ ПРОМПТИНГА:\n")
    print("1. zero_shot   — быстрая генерация без примеров")
    print("2. cov+cok     — с проверкой фактов (рекомендуется)")
    print("3. few_shot    — с примерами из базы знаний")

    while True:
        choice = input("\nВведите номер техники (1-3, Enter=2): ").strip() or "2"

        # Безопасная очистка UTF-8
        choice = choice.encode('utf-8', errors='ignore').decode('utf-8').strip() or "2"

        technique_map = {
            "1": "zero_shot",
            "2": "cov+cok",
            "3": "few_shot"
        }

        if choice in technique_map:
            return technique_map[choice]
        else:
            print("❌ Ошибка: введите 1, 2 или 3")


def generate_themes(bot, technique, focus=None):
    """Генерирует темы с напоминанием агенту про FileSearch и Web Search"""
    print(f"\n🔄 Генерация тем с техникой {technique}...")

    # Формируем input с напоминанием агенту
    if focus:
        user_input = f"Сгенерируй 10 тем с упором на: {focus}. Не забудь использовать FileSearch (загруженные файлы Богачев, CrossFit) и Web Search (свежие новости 2026)."
    else:
        user_input = "Сгенерируй 10 актуальных тем. Обязательно используй FileSearch (загруженные файлы) и Web Search (свежие новости 2026: CrossFit Open, ВОЗ, PubMed)."

    themes, usage = bot.generate_themes(technique=technique, custom_input=user_input)

    print_separator()
    print("📋 СГЕНЕРИРОВАННЫЕ ТЕМЫ:\n")
    print(themes)
    print_separator()

    # Выводим статистику токенов
    if usage:
        print_token_usage("Генерация тем", usage)

    return themes


def parse_theme_from_list(theme_choice: str, themes_text: str) -> str:
    """
    Парсит выбор темы и возвращает название темы.

    Args:
        theme_choice: ввод пользователя (номер или название)
        themes_text: текст со списком тем

    Returns:
        Название темы для генерации поста
    """
    # Если это номер (1-10), извлекаем тему из списка
    if theme_choice.isdigit():
        theme_num = int(theme_choice)
        if 1 <= theme_num <= 10:
            # Ищем строку с номером темы (например: "3️⃣ Тема [источник]")
            import re
            # Паттерн: цифра с эмодзи + текст до [источник]
            patterns = [
                rf"{theme_num}️⃣\s+([^\[]+)",  # "3️⃣ Тема [источник]"
                rf"{theme_num}\.\s+([^\[]+)",   # "3. Тема [источник]"
                rf"^{theme_num}[\.)\s]+([^\[]+)", # "3) Тема [источник]"
            ]

            for pattern in patterns:
                match = re.search(pattern, themes_text, re.MULTILINE)
                if match:
                    theme_name = match.group(1).strip()
                    # Убираем лишние символы в конце
                    theme_name = re.sub(r'\s*[\[\(].*$', '', theme_name).strip()
                    print(f"✅ Извлечена тема #{theme_num}: '{theme_name}'")
                    return theme_name

            # Если не нашли паттерн, возвращаем номер (агент попробует разобраться)
            print(f"⚠️ Не удалось извлечь тему #{theme_num} из списка, передаю номер")
            return theme_choice

    # Если это не номер, возвращаем как есть (пользовательская тема)
    return theme_choice


def get_theme_choice(themes):
    """Запрашивает выбор темы или ввод своей"""
    print("\n🎯 ВЫБЕРИТЕ ТЕМУ ДЛЯ ПОСТА:\n")
    print("• Введите номер темы (1-10)")
    print("• Или напишите свою тему")
    print("• Или 'новые' для генерации новых тем")

    while True:
        theme = input("\nВаш выбор: ").strip()

        # Безопасная очистка UTF-8 (удаляем суррогатные пары)
        theme = theme.encode('utf-8', errors='ignore').decode('utf-8').strip()

        if not theme:
            print("❌ Ошибка: введите номер темы, название или 'новые'")
            continue

        if theme.lower() in ['новые', 'new', 'n']:
            return 'regenerate'

        return theme


def get_post_length():
    """Запрашивает длину поста с валидацией"""
    print("\n📝 ДЛИНА ПОСТА:\n")
    print("• По умолчанию: 500 символов")
    print("• Рекомендуется: 500-700 символов")
    print("• Диапазон: 200-1000 символов")

    while True:
        length_input = input("\nДлина в символах (Enter=500): ").strip()

        if not length_input:
            return 500

        try:
            post_length = int(length_input)
            if 200 <= post_length <= 1000:
                return post_length
            else:
                print("❌ Ошибка: длина должна быть от 200 до 1000 символов")
                print("💡 Рекомендуемый диапазон: 500-700 символов")
        except ValueError:
            print("❌ Ошибка: введите число от 200 до 1000")


def get_regenerate_focus():
    """Запрашивает фокус для перегенерации тем"""
    print("\n🎯 НА ЧТО СДЕЛАТЬ УПОР В НОВЫХ ТЕМАХ?\n")
    print("1. Питание и диета")
    print("2. Спорт и тренировки (CrossFit, силовые)")
    print("3. Сон и восстановление")
    print("4. Техника упражнений (гимнастика, олимпийская атлетика)")
    print("5. Здоровье и профилактика (ВОЗ, исследования)")
    print("6. Разное (без фокуса)")

    choice = input("\nВведите номер (1-6, Enter=6): ").strip() or "6"

    # Безопасная очистка UTF-8
    choice = choice.encode('utf-8', errors='ignore').decode('utf-8').strip() or "6"

    focus_map = {
        "1": "питание, диета, спортивное питание",
        "2": "спорт, тренировки, CrossFit, силовые упражнения, меткон",
        "3": "сон, восстановление, регенерация",
        "4": "техника упражнений, гимнастика, олимпийская атлетика, прогрессии",
        "5": "здоровье, профилактика, рекомендации ВОЗ, научные исследования",
        "6": None  # без фокуса
    }

    return focus_map.get(choice, None)


def save_post(post, theme, technique):
    """Сохраняет пост в файл"""
    output_dir = Path(__file__).parent.parent / "output" / "posts"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Безопасное имя файла из темы
    safe_theme = "".join(c if c.isalnum() or c in (' ', '_') else '_' for c in theme[:30])
    safe_theme = safe_theme.strip().replace(' ', '_')

    filename = f"post_{technique}_{safe_theme}_{timestamp}.md"
    filepath = output_dir / filename

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"# {theme}\n\n")
        f.write(f"**Техника**: {technique}\n")
        f.write(f"**Дата**: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n")
        f.write(f"**Длина**: {len(post)} символов\n\n")
        f.write("---\n\n")
        f.write(post)

    return filepath


def get_next_action():
    """Запрашивает следующее действие после генерации поста"""
    print("\n\n" + "="*70)
    print("✅ ПОСТ ГОТОВ!")
    print("="*70)
    print("\n🎯 ЧТО ДЕЛАТЬ ДАЛЬШЕ?\n")
    print("1. Закончить работу")
    print("2. Сгенерировать новый пост на эту же тему")
    print("3. Сгенерировать пост на другую тему (из текущего списка)")
    print("4. Сгенерировать новый список тем")

    while True:
        choice = input("\nВведите номер (1-4): ").strip()

        # Безопасная очистка UTF-8
        choice = choice.encode('utf-8', errors='ignore').decode('utf-8').strip()

        if choice in ['1', '2', '3', '4']:
            return choice
        else:
            print("❌ Ошибка: введите 1, 2, 3 или 4")


def main():
    """Основная функция с интерактивным меню"""
    print("\n" + "="*70)
    print("🤖 NATRIUM FITNESS — ГЕНЕРАТОР ПОСТОВ")
    print("="*70)

    # Инициализация бота
    try:
        bot = NatriumBot()
    except ValueError as e:
        print(f"\n❌ Ошибка инициализации: {e}")
        print("💡 Проверьте файл .env (YANDEX_AGENT_ID, YANDEX_CLOUD_API_KEY, YANDEX_FOLDER_ID)")
        return

    # 1. Выбор техники промптинга
    print_separator()
    technique = get_technique_choice()
    print(f"\n✅ Выбрана техника: {technique}")

    # 2. Генерация тем
    print_separator()
    themes = generate_themes(bot, technique)

    # Основной цикл работы
    while True:
        # 3. Выбор темы или перегенерация
        theme_choice = get_theme_choice(themes)

        if theme_choice == 'regenerate':
            # Перегенерация тем с фокусом
            focus = get_regenerate_focus()
            print_separator()
            themes = generate_themes(bot, technique, focus)
            continue

        # Парсим тему (для красивого вывода и имени файла)
        theme_name = parse_theme_from_list(theme_choice, themes)

        # 4. Длина поста
        print_separator()
        post_length = get_post_length()
        print(f"\n✅ Длина поста: {post_length} символов")

        # 5. Генерация поста
        print_separator()
        print(f"\n✍️ Генерация поста на тему: '{theme_name}'")
        print(f"📊 Техника: {technique}, Длина: {post_length} символов\n")

        try:
            # Передаём название темы (из парсинга) в упрощённом формате
            # Формат: "Сгенерируй пост на тему X с использованием техники Y"
            post, usage = bot.generate_post(
                theme=theme_name,  # название темы (из парсинга)
                technique=technique,
                post_length=post_length
            )

            print_separator()
            print("📄 СГЕНЕРИРОВАННЫЙ ПОСТ:\n")
            print(post)
            print_separator()

            # Сохранение поста
            filepath = save_post(post, theme_name, technique)
            print(f"\n💾 Пост сохранён: {filepath.relative_to(Path.cwd())}")

            # Выводим статистику токенов
            if usage:
                print_token_usage("Генерация поста", usage)

        except Exception as e:
            print(f"\n❌ Ошибка при генерации поста: {e}")
            print("💡 Попробуйте ещё раз или выберите другую тему")
            continue

        # 6. Внутренний цикл для меню "Что делать дальше?"
        while True:
            next_action = get_next_action()

            if next_action == '1':
                # Закончить — выходим из ВСЕХ циклов
                print("\n👋 Спасибо за работу! До встречи!")
                print_separator()
                return  # выход из функции main()

            elif next_action == '2':
                # Новый пост на ту же тему — генерируем и остаёмся в внутреннем цикле
                print_separator()
                print(f"\n🔄 Генерация нового поста на тему: '{theme_name}'")

                # Спрашиваем длину
                post_length = get_post_length()
                print(f"\n✅ Длина поста: {post_length} символов")

                print_separator()
                print(f"\n✍️ Генерация нового поста...")
                print(f"📊 Техника: {technique}, Длина: {post_length} символов\n")

                # Генерируем пост на ТУ ЖЕ тему
                try:
                    post, usage = bot.generate_post(
                        theme=theme_name,
                        technique=technique,
                        post_length=post_length
                    )

                    print_separator()
                    print("📄 СГЕНЕРИРОВАННЫЙ ПОСТ:\n")
                    print(post)
                    print_separator()

                    # Сохраняем
                    filepath = save_post(post, theme_name, technique)
                    print(f"\n💾 Пост сохранён: {filepath.relative_to(Path.cwd())}")

                    # Выводим статистику токенов
                    if usage:
                        print_token_usage("Генерация поста", usage)

                except Exception as e:
                    print(f"\n❌ Ошибка при генерации поста: {e}")
                    print("💡 Попробуйте ещё раз")

                # Остаёмся в внутреннем цикле — покажем меню снова

            elif next_action == '3':
                # Пост на другую тему — выходим из внутреннего цикла
                print_separator()
                print("📋 Текущий список тем:\n")
                print(themes)
                print_separator()
                break  # выход из внутреннего цикла while True

            elif next_action == '4':
                # Новый список тем — выходим из внутреннего цикла
                focus = get_regenerate_focus()
                print_separator()
                themes = generate_themes(bot, technique, focus)
                break  # выход из внутреннего цикла while True


if __name__ == "__main__":
    main()

