import os
from dotenv import load_dotenv
from pathlib import Path
import httpx
import json
import logging

# Настройка логирования
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

class NatriumBot:
    def __init__(self, prompts_dir: str = "prompts"):
        self.api_key = os.getenv("YANDEX_CLOUD_API_KEY")
        self.folder_id = os.getenv("YANDEX_FOLDER_ID")
        self.agent_id = os.getenv("YANDEX_AGENT_ID")
        self.prompts_dir = Path(__file__).parent.parent / prompts_dir
        
        # Создаем HTTP клиент с увеличенным таймаутом (для длинных тем)
        self.http_client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=10.0),
            headers={
                "Authorization": f"Api-Key {self.api_key}",
                "x-folder-id": self.folder_id,
                "Content-Type": "application/json"
            }
        )
        
        self.base_url = "https://rest-assistant.api.cloud.yandex.net/v1"
    
    def update_agent_prompt(self, prompt_file: str = "agent_system_prompt.md") -> bool:
        """Обновляет системный промпт агента в Yandex Cloud
        
        Args:
            prompt_file: имя файла с промптом в prompts_dir
            
        Returns:
            bool: True если обновление успешно
        """
        try:
            # Читаем новый промпт из файла
            prompt_path = self.prompts_dir / prompt_file
            if not prompt_path.exists():
                logger.error(f"Prompt file not found: {prompt_path}")
                return False
            
            with open(prompt_path, 'r', encoding='utf-8') as f:
                new_prompt = f.read()
            
            logger.info(f"Updating agent {self.agent_id} with prompt from {prompt_file}")
            logger.info(f"Prompt length: {len(new_prompt)} chars")
            
            # Обновляем агента через API
            payload = {
                "prompt": new_prompt,
                "name": "Natrium SMM Bot"  # Можно задать имя агента
            }
            
            response = self.http_client.patch(
                f"{self.base_url}/agents/{self.agent_id}",
                json=payload
            )
            response.raise_for_status()
            
            logger.info(f"✅ Agent prompt updated successfully!")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to update agent prompt: {e}")
            return False
    
    def _format_variables(self, variables: dict) -> str:
        """Форматирует переменные в строку для additional_instructions"""
        if not variables:
            return ""
        lines = ["Используй следующие переменные:"]
        for key, value in variables.items():
            lines.append(f"{key}: {value}")
        return "\n".join(lines)
    

    def generate_themes(self, technique: str = "cov+cok", custom_input: str = None, previous_themes: list = None) -> tuple:
        """Генерирует 10 тем (пустая USER_THEME)

        Args:
            technique: техника генерации
            custom_input: пользовательский input (опционально)
            previous_themes: список предыдущих тем для избежания повторений

        Returns:
            tuple: (themes_text, usage_dict)
        """
        import time

        variables = {
            "TECHNIQUE": technique,
            "USER_THEME": "",
            "POST_LENGTH": "500"
        }
        
        # Добавляем timestamp для рандомизации
        timestamp_hint = f"Запрос #{int(time.time() % 10000)}"
        
        # Формируем список предыдущих тем для избежания повторений
        previous_themes_text = ""
        if previous_themes and len(previous_themes) > 0:
            previous_themes_text = f"""
❌ НЕ ИСПОЛЬЗУЙ ЭТИ ТЕМЫ (они уже были сгенерированы):
{chr(10).join([f'- {theme}' for theme in previous_themes])}

ГЕНЕРИРУЙ ПОЛНОСТЬЮ НОВЫЕ ТЕМЫ, НЕ ПОХОЖИЕ НА ПРЕДЫДУЩИЕ!
"""

        # Формируем явную инструкцию
        if custom_input:
            input_text = f"""ЗАДАЧА: {custom_input}

ВАЖНО:
- Это запрос на ГЕНЕРАЦИЮ ТЕМ, НЕ поста!
- USER_THEME = "" (пустая строка)
- Каждая тема НЕ БОЛЕЕ 5 СЛОВ
- Темы на ОБЫЧНОМ регистре (не CAPS!)
- Добавь источники в [квадратных скобках]
{previous_themes_text}
🔥 ДЛЯ РАЗНООБРАЗИЯ И НОВИЗНЫ:
- ОБЯЗАТЕЛЬНО используй книгу "Большая книга о соцсетях" в FileSearch
- Применяй рекомендации из книги: вирусные форматы, хуки, триггеры
- Генерируй НЕСТАНДАРТНЫЕ темы (не только базовые упражнения/питание)
- Используй актуальные тренды 2026 года из Web Search
- Миксуй форматы: вопросы, мифы, инсайты, кейсы, челленджи

⚠️ КРИТИЧЕСКИ ВАЖНО - НЕ ПОВТОРЯЙСЯ:
- Генерируй СОВЕРШЕННО РАЗНЫЕ темы при каждом запросе
- НЕ используй шаблонные темы (Подготовка к Open, Техника гребли, Электролиты...)
- Проявляй КРЕАТИВНОСТЬ и ОРИГИНАЛЬНОСТЬ
- Каждый набор тем должен быть УНИКАЛЬНЫМ
- Запрос: {timestamp_hint}

Формат вывода:
1️⃣ Тема 1 [источник]
2️⃣ Тема 2 [источник]
...
🔟 Тема 10 [источник]"""
        else:
            input_text = f"""ЗАДАЧА: Сгенерируй 10 АКТУАЛЬНЫХ ТЕМ для постов.

ВАЖНО:
- Это запрос на ГЕНЕРАЦИЮ ТЕМ, НЕ поста!
- USER_THEME = "" (пустая строка)
- Каждая тема НЕ БОЛЕЕ 5-7 СЛОВ
- Темы на ОБЫЧНОМ регистре (не CAPS!)
- Добавь источники в [квадратных скобках]
{previous_themes_text}
🔍 ПОРЯДОК РАБОТЫ:
1. Используй Web Search для поиска АКТУАЛЬНЫХ тем 2026 года
2. Используй File Search для поиска ИНТЕРЕСНОГО материала
3. ОБЯЗАТЕЛЬНО найди в File Search книгу "Большая книга о соцсетях"
4. Пропусти ВСЕ темы через рекомендации из книги о соцсетях

🔥 ФОРМАТЫ ТЕМ (используй разнообразные):
- Провокационные вопросы: "периодизация: миф или реальность?"
- Формат "как": "как гребля concept2 меняет тело"
- Секреты/инсайты: "crossfit open 2026: секреты подготовки"
- Развенчание мифов: "питание атлета: развенчиваем мифы"
- Персонализация: "твой личный алгоритм успеха"
- Конкретные цифры: "сон атлета: 7-9 часов"

⚠️ ЗАПРЕЩЕНО:
- Скучные базовые темы ("техника гребли", "периодизация нагрузок")
- Только название упражнения без хука
- Темы без эмоционального триггера

⚠️ КРИТИЧЕСКИ ВАЖНО - НЕ ПОВТОРЯЙСЯ:
- Генерируй СОВЕРШЕННО РАЗНЫЕ темы при каждом запросе
- НЕ используй шаблонные темы (Подготовка к Open, Техника гребли, Электролиты...)
- Проявляй КРЕАТИВНОСТЬ и ОРИГИНАЛЬНОСТЬ
- Каждый набор тем должен быть УНИКАЛЬНЫМ
- Запрос: {timestamp_hint}

Формат вывода:
1️⃣ Тема 1 [источник]
2️⃣ Тема 2 [источник]
...
🔟 Тема 10 [источник]"""

        return self._call_api(variables, input_text=input_text)

    def generate_post(self, theme: str, technique: str = "cov+cok", post_length: int = 500) -> tuple:
        """Генерирует пост по теме

        Returns:
            tuple: (post_text, usage_dict)
        """
        variables = {
            "TECHNIQUE": technique,
            "USER_THEME": theme,
            "POST_LENGTH": str(post_length)
        }
        
        # КРИТИЧЕСКИ ВАЖНО: явно указываем, что это запрос на ПОСТ, а не темы
        input_text = f"""⚠️⚠️⚠️ КРИТИЧЕСКИ ВАЖНО ⚠️⚠️⚠️

ЗАПРЕЩЕНО выводить рассуждения, планы работы, вызовы функций!
НЕ выводи текст типа: "🔄 Сначала мне нужно собрать информацию...", "[Вызов функции...]"
ВЫВОДИ ТОЛЬКО ГОТОВЫЙ ПОСТ!

Сгенерируй пост на тему "{theme}" с применением данных из File Search и Web Search с применением Chain of Knowledge и перепроверкой фактов cov+cok

⚠️ ВАЖНО:
- USER_THEME = "{theme}" (НЕ пустая строка!)
- Это запрос на ГЕНЕРАЦИЮ ПОСТА, НЕ тем!
- Длина: {post_length} символов
- Техника: {technique}

🔍 ПОРЯДОК РАБОТЫ (применяй CoV+CoK):
1. Используй File Search для получения материалов по теме (Богачева, CrossFit, книга о соцсетях)
2. Выполни Web Search для поиска актуальной информации 2026 года, исследований и конкретных цифр
3. Проверь полученные данные через верификацию источников (WHO, CrossFit.com, PubMed, научные исследования)
4. Сгенерируй пост, соблюдая все заданные требования к стилю и структуре из системного промпта

🎯 ОБЯЗАТЕЛЬНАЯ СТРУКТУРА ПОСТА (КАЖДЫЙ ПОСТ НАЧИНАЕТСЯ ТАК):

⚠️⚠️⚠️ АЛГОРИТМ ГЕНЕРАЦИИ (СТРОГО ПО ПОРЯДКУ):
1. СНАЧАЛА сформируй ЗАГОЛОВОК по шаблону ниже (тема в заголовке = "{theme}")
2. ЗАТЕМ напиши ЛИД-ЗАТРАВКУ по шаблону ниже
3. ТОЛЬКО ПОСЛЕ ЭТОГО переходи к секциям 3-9

⚠️ Если объём приближается к {post_length} символам - сокращай секции 3-5, НО СОХРАНЯЙ заголовок и лид ПОЛНОСТЬЮ!

1️⃣ ЗАГОЛОВОК (СТРОГО ПЕРВАЯ СТРОКА ПОСТА, БЕЗ ПРОПУСКОВ!):
   • Формат: [эмодзи] **[ТЕМА В CAPS]: [КЛЮЧЕВАЯ ИДЕЯ 3-7 СЛОВ]**
   • ТЕМА В ЗАГОЛОВКЕ ОБЯЗАНА СООТВЕТСТВОВАТЬ "{theme}"!
   • Примеры правильных заголовков:
     - Для темы "гребля" → 💪 **ГРЕБЛЯ CONCEPT2: КАЛОРИИ ГОРЯТ КАК НИКОГДА**
     - Для темы "регенерация" → 🔥 **РЕГЕНЕРАЦИЯ: ПОЧЕМУ ОТДЫХ ВАЖНЕЕ ТРЕНИРОВКИ**
     - Для темы "профилактика травм" → 🛡️ **ПРОФИЛАКТИКА ТРАВМ: КАК ТРЕНИРОВАТЬСЯ БЕЗ РИСКА**
   • ❌ НЕПРАВИЛЬНО: начинать с "🔥 ФАКТОРЫ..." без заголовка темы
   • ❌ НЕПРАВИЛЬНО: тема в заголовке не совпадает с "{theme}"

2️⃣ ЛИД (ЗАТРАВКА - ОБЯЗАТЕЛЬНО ПОСЛЕ ЗАГОЛОВКА, 1-3 ПРЕДЛОЖЕНИЯ):
   • ЭТО КРЮЧОК ДЛЯ ЧИТАТЕЛЯ - ОБЯЗАТЕЛЕН ДАЖЕ ДЛЯ ТЕХНИЧЕСКИХ ТЕМ!
   • Форматы лида:
     - Прямое обращение: "Часто слышу...", "Сталкивались?", "Знакомая ситуация?"
     - Провокационный вопрос или заблуждение
     - Конкретная ситуация из жизни зала
     - Парадокс: "Казалось бы, X. Но на самом деле Y!"
     - Личный опыт: "В нашем зале мы часто видим..."
   • Примеры правильных лидов:
     - "Знакомо: после убойной тренировки не можешь пошевелиться два дня?"
     - "Часто слышу: 'Восстановление - это просто лежать!' На самом деле это наука."
     - "Казалось бы, кроссфит травмоопасен. Но статистика показывает обратное!"
     - "А вы знали, что 80% травм - результат неправильной разминки?"
   • ❌ НЕПРАВИЛЬНО: начинать сразу с "🔥 ФАКТОРЫ" без лида
   • ⚠️ Лид НЕЛЬЗЯ ПРОПУСКАТЬ НИ ПРИ КАКИХ УСЛОВИЯХ!

3️⃣ ОСНОВНЫЕ СЕКЦИИ (ТОЛЬКО ПОСЛЕ ЗАГОЛОВКА И ЛИДА):
   • 🔥 **[НАЗВАНИЕ]**: факты с источниками
   • 📊 **ФАКТЫ**: цифры и статистика
   • 💓 **ПРАКТИКА**: применение в Натриум
   • ✅ **ВЫВОДЫ**: основная мысль
   
4️⃣ CTA + СЛОГАН + ХЕШТЕГИ

🔥 КРИТИЧЕСКИ ВАЖНО:
- ⚠️ ГЕНЕРИРОВАТЬ ПОСТ БЕЗ ЗАГОЛОВКА И ЛИДА ЗАПРЕЩЕНО, ДАЖЕ ЕСЛИ ОБЪЁМ БЛИЗОК К ЛИМИТУ!
- ⚠️ ЗАМЕНЯТЬ ЗАГОЛОВОК ИЛИ ЛИД НА ДРУГИЕ СЕКЦИИ ЗАПРЕЩЕНО!
- ⚠️ НАЧАЛО С "🔥 ФАКТОРЫ..." БЕЗ ЗАГОЛОВКА = ОШИБКА!
🔥 КРИТИЧЕСКИ ВАЖНО:
- ⚠️ ГЕНЕРИРОВАТЬ ПОСТ БЕЗ ЗАГОЛОВКА И ЛИДА ЗАПРЕЩЕНО, ДАЖЕ ЕСЛИ ОБЪЁМ БЛИЗОК К ЛИМИТУ!
- ⚠️ ЗАМЕНЯТЬ ЗАГОЛОВОК ИЛИ ЛИД НА ДРУГИЕ СЕКЦИИ ЗАПРЕЩЕНО!
- ⚠️ НАЧАЛО С "🔥 ФАКТОРЫ..." БЕЗ ЗАГОЛОВКА = ОШИБКА!
- Заголовки секций ОБЯЗАТЕЛЬНО в ** (например: 🔥 **НАЗВАНИЕ СЕКЦИИ:**)
- Каждый факт с источником в формате [источник](URL) или (источник)
- Без маркера ">" в начале (не цитата!)
- НЕ выводи рассуждения и планы работы!

⚙️ ПОРЯДОК ДЕЙСТВИЙ:
1. Сначала сформируй ЗАГОЛОВОК с темой "{theme}"
2. Затем напиши ЛИД-ЗАТРАВКУ (обязательно!)
3. Только потом переходи к секциям

НАЧИНАЙ ПОСТ С ЗАГОЛОВКА В CAPS, ПОТОМ ЛИД, ПОТОМ СЕКЦИИ!

ПРАВИЛЬНЫЙ ПРИМЕР ДЛЯ ТЕМЫ "Регенерация после интенсивных тренировок":

🔥 **РЕГЕНЕРАЦИЯ: СЕКРЕТ ПОСТОЯННОГО ПРОГРЕССА**

Знакомо: после убойной тренировки не можешь пошевелиться два дня? Многие думают, что это нормально. На самом деле правильное восстановление - это 50% успеха.

🔥 **ФАКТОРЫ РЕГЕНЕРАЦИИ:**
• Качественный сон...
..."""

        # Добавляем ключевые инструкции для каждой техники
        if technique == "cov+cok":
            input_text += """\n\n✅ После сбора и проверки информации сгенерируй пост по структуре из системного промпта"""

        elif technique == "few_shot":
            input_text += """\n\n✅ Few-Shot требования:
- Изучи примеры постов в FileSearch
- Используй их структуру и стиль
- Сохрани тон Натриум Фитнесс"""

        return self._call_api(variables, input_text=input_text)

    def _call_api(self, variables: dict, input_text: str = "Выполни задачу") -> tuple:
        """Выполняет запрос к API Yandex Cloud Assistant

        Returns:
            tuple: (result_text, usage_dict) где usage_dict содержит inputTextTokens, completionTokens, totalTokens
        """
        try:
            # Безопасная обработка UTF-8 (удаляем суррогатные пары)
            input_text = input_text.encode('utf-8', errors='ignore').decode('utf-8')

            # Прямой REST API запрос к Yandex
            payload = {
                "prompt": {
                    "id": self.agent_id,
                    "variables": variables or {}
                },
                "input": input_text
            }
            
            response = self.http_client.post(
                f"{self.base_url}/responses",
                json=payload
            )
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"🔍 DEBUG: API response keys: {data.keys()}")
            logger.info(f"🔍 DEBUG: Full response: {data}")
            
            # Правильная структура Yandex API response
            result = ""
            if "output" in data and len(data["output"]) > 0:
                output_item = data["output"][0]
                if "content" in output_item and len(output_item["content"]) > 0:
                    content_item = output_item["content"][0]
                    result = content_item.get("text", "")
            
            # Fallback на старые поля если структура другая
            if not result:
                result = data.get("output_text", "")
            if not result:
                result = data.get("text", "")
                
            logger.info(f"🔍 DEBUG: Extracted result length: {len(result) if result else 0}")

            # Извлекаем usage данные (если доступны)
            usage = {}
            if "usage" in data:
                usage_data = data["usage"]
                usage = {
                    'input_tokens': usage_data.get('input_tokens', 0),
                    'output_tokens': usage_data.get('output_tokens', 0),
                    'total_tokens': usage_data.get('total_tokens', 0),
                    'input_tokens_details': usage_data.get('input_tokens_details'),
                    'output_tokens_details': usage_data.get('output_tokens_details')
                }

            return result, usage

        except Exception as e:
            logger.error(f"❌ ОШИБКА API: {e}")
            raise
