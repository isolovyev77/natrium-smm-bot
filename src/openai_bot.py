import os
from dotenv import load_dotenv
from pathlib import Path
import logging
from openai import OpenAI

# Настройка логирования
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

class OpenAIBot:
    """
    Класс для работы с OpenAI Responses API.
    Использует hosted tools: file_search + web_search.
    """
    
    def __init__(self, prompts_dir: str = "prompts"):
        self.api_key = os.getenv("OPENAI_API_KEY")
        
        # 2-шаговый пайплайн: разные модели для разных задач
        self.themes_model = os.getenv("OPENAI_THEMES_MODEL", "gpt-4o-mini")  # Быстро, дешево
        self.post_model = os.getenv("OPENAI_POST_MODEL", "gpt-5.2")  # Качество, креатив
        
        # Vector Store ID (если есть - используем file_search)
        self.vector_store_id = os.getenv("OPENAI_VECTOR_STORE_ID")
        
        self.prompts_dir = Path(__file__).parent.parent / prompts_dir
        
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        
        self.client = OpenAI(api_key=self.api_key)
        
        # Загружаем системный промпт
        self.system_prompt = self._load_system_prompt()
        
        # Определяем доступные инструменты
        self.available_tools = self._check_available_tools()
        
        logger.info(f"OpenAIBot initialized:")
        logger.info(f"  Themes: {self.themes_model}")
        logger.info(f"  Posts: {self.post_model}")
        logger.info(f"  Tools: {', '.join(self.available_tools) if self.available_tools else 'None'}")
    
    def _check_available_tools(self) -> list:
        """Проверяет какие tools доступны"""
        tools = []
        
        # File Search доступен если есть Vector Store
        if self.vector_store_id:
            tools.append("file_search")
            logger.info(f"  Vector Store: {self.vector_store_id}")
        
        # Web Search - проверим при первом запросе
        # (оставляем возможность добавить позже)
        
        return tools
    
    def _load_system_prompt(self, prompt_file: str = "agent_system_prompt.md") -> str:
        """Загружает системный промпт из файла"""
        prompt_path = self.prompts_dir / prompt_file
        
        if not prompt_path.exists():
            logger.warning(f"Prompt file not found: {prompt_path}, using default")
            return "Ты эксперт по CrossFit, фитнесу и здоровому образу жизни."
        
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read()
    
    def generate_themes(self, technique: str = "cov+cok", custom_input: str = None, previous_themes: list = None) -> tuple:
        """
        Генерирует 10 тем для постов.
        
        Args:
            technique: техника генерации (не используется в варианте A, для совместимости)
            custom_input: пользовательский запрос (фокус на категорию)
            previous_themes: список предыдущих тем для избежания повторений
        
        Returns:
            tuple: (themes_text, usage_dict)
        """
        import time
        
        # Формируем запрос с учетом предыдущих тем
        timestamp_hint = f"Запрос #{int(time.time() % 10000)}"
        
        previous_themes_text = ""
        if previous_themes and len(previous_themes) > 0:
            previous_themes_text = f"""
❌ НЕ ИСПОЛЬЗУЙ ЭТИ ТЕМЫ (они уже были сгенерированы):
{chr(10).join([f'- {theme}' for theme in previous_themes])}

ГЕНЕРИРУЙ ПОЛНОСТЬЮ НОВЫЕ ТЕМЫ, НЕ ПОХОЖИЕ НА ПРЕДЫДУЩИЕ!
"""
        
        # Формируем промпт для генерации тем
        if custom_input:
            user_prompt = f"""ЗАДАЧА: {custom_input}

ВАЖНО:
- Это запрос на ГЕНЕРАЦИЮ ТЕМ, НЕ поста!
- Каждая тема НЕ БОЛЕЕ 5-7 слов
- Темы на ОБЫЧНОМ регистре (не CAPS!)
- Добавь краткое описание источника в [квадратных скобках]
{previous_themes_text}

🔥 ФОРМАТЫ ТЕМ (используй разнообразные):
- Провокационные вопросы: "периодизация: миф или реальность?"
- Формат "как": "как гребля concept2 меняет тело"
- Секреты/инсайты: "crossfit open 2026: секреты подготовки"
- Развенчание мифов: "питание атлета: развенчиваем мифы"
- Конкретные цифры: "сон атлета: 7-9 часов"

⚠️ КРИТИЧЕСКИ ВАЖНО - НЕ ПОВТОРЯЙСЯ:
- Генерируй СОВЕРШЕННО РАЗНЫЕ темы при каждом запросе
- НЕ используй шаблонные темы
- Проявляй КРЕАТИВНОСТЬ и ОРИГИНАЛЬНОСТЬ
- Запрос: {timestamp_hint}

Формат вывода:
1️⃣ Тема 1 [источник]
2️⃣ Тема 2 [источник]
...
🔟 Тема 10 [источник]"""
        else:
            user_prompt = f"""ЗАДАЧА: Сгенерируй 10 АКТУАЛЬНЫХ ТЕМ для постов о CrossFit, фитнесе и здоровье.

ВАЖНО:
- Это запрос на ГЕНЕРАЦИЮ ТЕМ, НЕ поста!
- Каждая тема НЕ БОЛЕЕ 5-7 слов
- Темы на ОБЫЧНОМ регистре (не CAPS!)
- Добавь краткое описание источника в [квадратных скобках]
{previous_themes_text}

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
- НЕ используй шаблонные темы
- Проявляй КРЕАТИВНОСТЬ и ОРИГИНАЛЬНОСТЬ
- Запрос: {timestamp_hint}

Формат вывода:
1️⃣ Тема 1 [источник]
2️⃣ Тема 2 [источник]
...
🔟 Тема 10 [источник]"""
        
        return self._call_api(user_prompt, model=self.themes_model)
    
    def generate_post(self, theme: str, technique: str = "cov+cok", post_length: int = 500) -> tuple:
        """
        Генерирует пост по теме.
        
        Args:
            theme: тема поста
            technique: техника генерации (не используется в варианте A)
            post_length: желаемая длина поста в символах
        
        Returns:
            tuple: (post_text, usage_dict)
        """
        
        user_prompt = f"""⚠️⚠️⚠️ КРИТИЧЕСКИ ВАЖНО ⚠️⚠️⚠️

ЗАПРЕЩЕНО выводить рассуждения, планы работы!
ВЫВОДИ ТОЛЬКО ГОТОВЫЙ ПОСТ!

Сгенерируй пост на тему "{theme}"

⚠️ ВАЖНО:
- Длина: примерно {post_length} символов
- Тема поста: "{theme}"

🎯 ОБЯЗАТЕЛЬНАЯ СТРУКТУРА ПОСТА:

⚠️⚠️⚠️ АЛГОРИТМ ГЕНЕРАЦИИ (СТРОГО ПО ПОРЯДКУ):
1. СНАЧАЛА сформируй ЗАГОЛОВОК
2. ЗАТЕМ напиши ЛИД-ЗАТРАВКУ
3. ТОЛЬКО ПОСЛЕ ЭТОГО переходи к секциям

⚠️ Если объём приближается к {post_length} символам - сокращай секции, НО СОХРАНЯЙ заголовок и лид ПОЛНОСТЬЮ!

1️⃣ ЗАГОЛОВОК (СТРОГО ПЕРВАЯ СТРОКА ПОСТА):
   • Формат: [эмодзи] **[ТЕМА В CAPS]: [КЛЮЧЕВАЯ ИДЕЯ 3-7 СЛОВ]**
   • ТЕМА В ЗАГОЛОВКЕ ОБЯЗАНА СООТВЕТСТВОВАТЬ "{theme}"!
   • Примеры:
     - 💪 **ГРЕБЛЯ CONCEPT2: КАЛОРИИ ГОРЯТ КАК НИКОГДА**
     - 🔥 **РЕГЕНЕРАЦИЯ: ПОЧЕМУ ОТДЫХ ВАЖНЕЕ ТРЕНИРОВКИ**
   • ❌ НЕПРАВИЛЬНО: начинать без заголовка темы

2️⃣ ЛИД (ЗАТРАВКА - ОБЯЗАТЕЛЬНО ПОСЛЕ ЗАГОЛОВКА, 1-3 ПРЕДЛОЖЕНИЯ):
   • ЭТО КРЮЧОК ДЛЯ ЧИТАТЕЛЯ - ОБЯЗАТЕЛЕН!
   • Форматы:
     - Прямое обращение: "Часто слышу...", "Сталкивались?"
     - Провокационный вопрос
     - Парадокс: "Казалось бы, X. Но на самом деле Y!"
   • Примеры:
     - "Знакомо: после тренировки не можешь пошевелиться два дня?"
     - "Часто слышу: 'Восстановление - это просто лежать!' На самом деле это наука."
   • ⚠️ Лид НЕЛЬЗЯ ПРОПУСКАТЬ!

3️⃣ ОСНОВНЫЕ СЕКЦИИ (ТОЛЬКО ПОСЛЕ ЗАГОЛОВКА И ЛИДА):
   • 🔥 **[НАЗВАНИЕ]**: факты и информация
   • 📊 **ФАКТЫ**: цифры и статистика
   • 💓 **ПРАКТИКА**: применение в Натриум Фитнесс
   • ✅ **ВЫВОДЫ**: основная мысль
   
4️⃣ CTA + СЛОГАН + ХЕШТЕГИ

🔥 КРИТИЧЕСКИ ВАЖНО:
- ⚠️ ГЕНЕРИРОВАТЬ ПОСТ БЕЗ ЗАГОЛОВКА И ЛИДА ЗАПРЕЩЕНО!
- ⚠️ НАЧАЛО С СЕКЦИИ БЕЗ ЗАГОЛОВКА = ОШИБКА!
- Заголовки секций в ** (например: 🔥 **НАЗВАНИЕ СЕКЦИИ:**)
- Каждый факт подкреплен логикой
- Без маркера ">" в начале (не цитата!)
- НЕ выводи рассуждения!

НАЧИНАЙ ПОСТ С ЗАГОЛОВКА В CAPS, ПОТОМ ЛИД, ПОТОМ СЕКЦИИ!

ПРАВИЛЬНЫЙ ПРИМЕР:

🔥 **РЕГЕНЕРАЦИЯ: СЕКРЕТ ПОСТОЯННОГО ПРОГРЕССА**

Знакомо: после убойной тренировки не можешь пошевелиться два дня? Многие думают, что это нормально. На самом деле правильное восстановление - это 50% успеха.

🔥 **ФАКТОРЫ РЕГЕНЕРАЦИИ:**
• Качественный сон 7-9 часов - время роста мышц
• Питание в течение 30 минут после тренировки
• Активное восстановление - легкое кардио

💓 **ПРАКТИКА В НАТРИУМ:**
✅ Foam rolling после WOD
✅ Стретчинг 10-15 минут
✅ Контроль пульса

Восстанавливайся правильно - прогрессируй быстрее! 🔥

#натриумфитнес #crossfit #восстановление"""
        
        return self._call_api(user_prompt, model=self.post_model)
    
    def _call_api(self, user_prompt: str, model: str = None) -> tuple:
        """
        Выполняет запрос к OpenAI Responses API.
        
        Args:
            user_prompt: пользовательский промпт (input)
            model: модель для использования (если None - используется post_model)
        
        Returns:
            tuple: (result_text, usage_dict)
        """
        try:
            # Используем переданную модель или дефолтную
            selected_model = model or self.post_model
            
            # Безопасная обработка UTF-8
            user_prompt = user_prompt.encode('utf-8', errors='ignore').decode('utf-8')
            
            # Комбинируем системный промпт с пользовательским
            full_input = f"{self.system_prompt}\n\n{user_prompt}"
            
            # Формируем список tools
            tools = []
            if self.vector_store_id and "file_search" in self.available_tools:
                tools.append({
                    "type": "file_search",
                    "vector_store_ids": [self.vector_store_id]
                })
            
            logger.info(f"🔍 OpenAI Responses API call:")
            logger.info(f"   Model: {selected_model}")
            logger.info(f"   Tools: {[t['type'] for t in tools] if tools else 'None'}")
            
            # Responses API вызов
            response = self.client.responses.create(
                model=selected_model,
                input=full_input,
                tools=tools if tools else None,
                temperature=0.7,
                max_tokens=2000
            )
            
            # Извлекаем результат
            result = response.output_text.strip()
            
            # Извлекаем usage данные
            usage = {
                'input_tokens': response.usage.input_tokens if hasattr(response.usage, 'input_tokens') else 0,
                'output_tokens': response.usage.output_tokens if hasattr(response.usage, 'output_tokens') else 0,
                'total_tokens': response.usage.total_tokens if hasattr(response.usage, 'total_tokens') else 0
            }
            
            logger.info(f"✅ OpenAI response received: {len(result)} chars, {usage['total_tokens']} tokens")
            
            return result, usage
            
        except Exception as e:
            logger.error(f"❌ OpenAI Responses API error: {e}")
            raise
