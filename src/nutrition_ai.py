"""Изолированный AI-адаптер для оценки еды.

Адаптер не использует SMM-ассистентов и их промпты. Если отдельная модель не
настроена, контроллер переводит пользователя на ручной ввод. Числовая
``confidence`` намеренно не возвращается: оценка по фото всегда помечается как
приблизительная и подтверждается человеком до сохранения.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
from typing import Any


class NutritionAIUnavailable(RuntimeError):
    """AI-анализ не настроен или временно недоступен."""


class NutritionAIResponseError(ValueError):
    """Модель вернула ответ, который нельзя безопасно показать как черновик."""


class NutritionAITargetAmbiguous(ValueError):
    """Для исправления нужно явно выбрать одну из нескольких позиций."""

    def __init__(self, items: list[dict[str, Any]]):
        self.candidates = tuple(
            {"index": index, "name": str(item.get("name", "")).strip() or f"Позиция {index + 1}"}
            for index, item in enumerate(items)
        )
        super().__init__("Выберите блюдо, которое нужно исправить")


class NutritionAIIntentAmbiguous(ValueError):
    """Фраза может означать и дополнение, и уточнение текущей позиции."""


class NutritionAI:
    """OpenAI Responses adapter для фото и свободного описания еды.

    Args:
        api_key: отдельный ключ или ``OPENAI_API_KEY``.
        model: ``NUTRITION_VISION_MODEL`` или ``gpt-4o-mini``.
        timeout_seconds: общий таймаут AI-запроса.
        client: тестовый или заранее настроенный OpenAI client.

    Returns from analyze methods:
        ``{"items": [...], "questions": [...]}``. Каждый item содержит
        ``name``, ``weight_g``, ``portion_text``, ``calories``, ``protein_g``,
        ``fat_g``, ``carbs_g``, ``approximate``.
    """

    MAX_IMAGE_BYTES = 12 * 1024 * 1024
    _FOOD_FIELDS = (
        "name",
        "weight_g",
        "portion_text",
        "calories",
        "protein_g",
        "fat_g",
        "carbs_g",
        "approximate",
    )
    _ADD_COMMANDS = frozenset({"добавь", "добавить", "добавьте"})
    _AMBIGUOUS_ADD_WORDS = frozenset({"еще", "ещё"})
    _GRAMS_PATTERN = re.compile(
        r"(?<![\w.,+\-])([-+]?(?:\d+(?:[.,]\d+)?|nan|inf(?:inity)?))"
        r"\s*(?:г|гр|грамм|грамма|граммов)\b",
        re.IGNORECASE,
    )
    _MASS_ONLY_PATTERN = re.compile(
        r"\s*(?:масса\s*)?(\d+(?:[.,]\d+)?)\s*(?:г|гр|грамм|грамма|граммов)\s*[.!]?\s*",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        client: Any | None = None,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("NUTRITION_VISION_MODEL", "gpt-4o-mini")
        self.timeout_seconds = float(
            timeout_seconds or os.getenv("NUTRITION_AI_TIMEOUT_SECONDS", "30")
        )
        self.client = client
        if self.client is None and self.api_key:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise NutritionAIUnavailable("Пакет openai не установлен") from exc
            self.client = OpenAI(
                api_key=self.api_key,
                timeout=self.timeout_seconds,
                max_retries=0,
            )

    @property
    def available(self) -> bool:
        return self.client is not None

    def analyze_photo(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
        if not self.available:
            raise NutritionAIUnavailable("AI-анализ фото не настроен")
        if not image_bytes:
            raise ValueError("Получено пустое изображение")
        if len(image_bytes) > self.MAX_IMAGE_BYTES:
            raise ValueError("Изображение превышает допустимый размер 12 МБ")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        prompt = (
            "Определи блюда и продукты на фото. Сформируй только черновую оценку КБЖУ. "
            "Для каждого элемента укажи название, приблизительную массу в граммах, "
            "понятное описание порции, ккал, белки, жиры и углеводы. "
            "Если блюдо или порция неразличимы, добавь короткий вопрос в questions. "
            "Не ставь диагнозов, не назначай норм и не возвращай confidence."
        )
        return self._request(
            [
                {"type": "input_text", "text": prompt},
                {
                    "type": "input_image",
                    "image_url": f"data:{mime_type};base64,{encoded}",
                    "detail": "auto",
                },
            ]
        )

    def analyze_text(self, description: str) -> dict[str, Any]:
        if not self.available:
            raise NutritionAIUnavailable("AI-анализ текста не настроен")
        description = description.strip()
        if not description:
            raise ValueError("Описание еды не может быть пустым")
        prompt = (
            "Преобразуй описание приема пищи в черновую оценку КБЖУ. "
            "Не назначай норм. Если масса или размер порции не указаны, сделай "
            "примерную оценку, пометь item approximate=true и сохрани понятное "
            "описание порции. Если данных совсем недостаточно, задай вопрос.\n\n"
            f"Описание пользователя: {description}"
        )
        return self._request([{"type": "input_text", "text": prompt}])

    def apply_clarification(
        self,
        answer: str,
        current_items: list[dict[str, Any]],
        *,
        item_index: int | None = None,
    ) -> dict[str, Any]:
        """Применить ответ на AI-уточнение к исходным гипотезам.

        Обычный ответ исправляет выбранную позицию. Только явная команда
        ``добавь``/``добавить`` добавляет новые позиции. Это не даёт ответу
        вида ``Это гречневые хлопья...`` сохраниться рядом со старой ошибочной
        гипотезой. Фраза ``а ещё ...`` остаётся неоднозначной и должна быть
        уточнена контроллером.
        """
        answer = self._validate_instruction(answer)
        items = self._copy_current_items(current_items)
        intent = self._clarification_intent(answer)
        if intent == "ambiguous":
            raise NutritionAIIntentAmbiguous(
                "Уточните: добавить ещё одно блюдо или исправить текущее?"
            )
        if intent == "append":
            addition = self.analyze_text(answer)
            appended = [self._mark_ai_item(item) for item in addition["items"]]
            return {
                "items": items + appended,
                "questions": list(addition.get("questions") or []),
                "operation": "append",
                "target_index": None,
            }

        target_index = self._resolve_target_index(items, item_index)
        self._explicit_weight_grams(answer)
        correction = self._request_item_correction(
            answer,
            items[target_index],
        )
        replacement = self._finalize_replacement(
            correction["items"],
            instruction=answer,
            original=items[target_index],
        )
        revised = list(items)
        revised[target_index] = replacement
        return {
            "items": revised,
            "questions": list(correction.get("questions") or []),
            "operation": "replace",
            "target_index": target_index,
        }

    def correct_draft_item(
        self,
        instruction: str,
        current_items: list[dict[str, Any]],
        item_index: int | None = None,
    ) -> dict[str, Any]:
        """Вернуть одну AI-позицию для замены выбранного блюда черновика."""
        instruction = self._validate_instruction(instruction)
        items = self._copy_current_items(current_items)
        target_index = self._resolve_target_index(items, item_index)
        self._explicit_weight_grams(instruction)
        correction = self._request_item_correction(
            instruction,
            items[target_index],
        )
        if correction.get("questions"):
            raise NutritionAIResponseError("Для исправления блюда нужно дополнительное уточнение")
        return self._finalize_replacement(
            correction["items"],
            instruction=instruction,
            original=items[target_index],
        )

    def _request_item_correction(
        self,
        instruction: str,
        original: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.available:
            raise NutritionAIUnavailable("AI-исправление еды не настроено")
        food_fields = {
            key: original[key]
            for key in self._FOOD_FIELDS
            if key in original
        }
        prompt = (
            "Исправь одну выбранную позицию черновика по сообщению пользователя. "
            "Верни items ровно с одной полностью пересчитанной позицией. "
            "Не возвращай исходную гипотезу вторым item. Перечисленные в названии "
            "хлопья, семена, орехи и другие составляющие одного блюда оставь одной "
            "составной позицией, если пользователь явно не назвал их отдельными блюдами. "
            "Если пользователь уточнил только массу, сохрани название и состав позиции. "
            "Оценка КБЖУ приблизительная. Не приписывай ей USDA или иной справочный источник.\n\n"
            f"Исходная позиция: {json.dumps(food_fields, ensure_ascii=False)}\n"
            f"Исправление пользователя: {instruction}"
        )
        return self._request([{"type": "input_text", "text": prompt}])

    @staticmethod
    def _validate_instruction(instruction: str) -> str:
        cleaned = str(instruction).strip()
        if not cleaned:
            raise ValueError("Исправление не может быть пустым")
        return cleaned

    @staticmethod
    def _copy_current_items(current_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(current_items, list) or not current_items:
            raise ValueError("Нет блюд для исправления")
        if any(not isinstance(item, dict) for item in current_items):
            raise ValueError("Некорректный список блюд")
        return [dict(item) for item in current_items]

    @classmethod
    def _clarification_intent(cls, instruction: str) -> str:
        words = instruction.casefold().split()
        if not words:
            return "replace"
        first = words[0].strip(".,!?:;")
        if first in cls._ADD_COMMANDS:
            return "append"
        if first in cls._AMBIGUOUS_ADD_WORDS:
            return "ambiguous"
        if first == "а" and len(words) > 1:
            second = words[1].strip(".,!?:;")
            if second in cls._AMBIGUOUS_ADD_WORDS:
                return "ambiguous"
        return "replace"

    @staticmethod
    def _resolve_target_index(
        items: list[dict[str, Any]],
        item_index: int | None,
    ) -> int:
        if item_index is None:
            if len(items) > 1:
                raise NutritionAITargetAmbiguous(items)
            return 0
        if isinstance(item_index, bool) or not isinstance(item_index, int):
            raise ValueError("Индекс блюда должен быть целым числом")
        if item_index < 0 or item_index >= len(items):
            raise ValueError("Выбранное блюдо не найдено")
        return item_index

    @classmethod
    def _finalize_replacement(
        cls,
        replacement_items: list[dict[str, Any]],
        *,
        instruction: str,
        original: dict[str, Any],
    ) -> dict[str, Any]:
        if len(replacement_items) != 1:
            raise NutritionAIResponseError(
                "AI должен вернуть ровно одно исправленное блюдо"
            )
        replacement = cls._mark_ai_item(replacement_items[0])
        weight = cls._explicit_weight_grams(instruction)
        if weight is not None:
            returned_weight = replacement.get("weight_g")
            if not isinstance(returned_weight, (int, float)) or isinstance(returned_weight, bool):
                raise NutritionAIResponseError(
                    "AI не связал КБЖУ с массой исправленного блюда"
                )
            returned_weight = float(returned_weight)
            if not math.isfinite(returned_weight) or returned_weight <= 0:
                raise NutritionAIResponseError(
                    "AI не связал КБЖУ с массой исправленного блюда"
                )
            if not math.isclose(returned_weight, weight):
                scale = weight / returned_weight
                for field in ("calories", "protein_g", "fat_g", "carbs_g"):
                    replacement[field] = round(float(replacement[field]) * scale, 4)
            replacement["weight_g"] = weight
            replacement["portion_text"] = f"{weight:g} г"
        if cls._MASS_ONLY_PATTERN.fullmatch(instruction):
            replacement["name"] = str(original.get("name", "")).strip()
        return replacement

    @staticmethod
    def _mark_ai_item(item: dict[str, Any]) -> dict[str, Any]:
        marked = {
            key: item[key]
            for key in NutritionAI._FOOD_FIELDS
            if key in item
        }
        marked["approximate"] = True
        marked["calculation_method"] = "ai"
        return marked

    @classmethod
    def _explicit_weight_grams(cls, instruction: str) -> float | None:
        matches = list(cls._GRAMS_PATTERN.finditer(instruction))
        if len(matches) != 1:
            return None
        value = float(matches[0].group(1).replace(",", "."))
        if not math.isfinite(value) or value <= 0 or value > 100000:
            raise ValueError("Недопустимое значение: масса")
        return value

    def _request(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        schema_instruction = (
            "Верни строго JSON: "
            '{"items":[{"name":"","weight_g":0,"portion_text":"",'
            '"calories":0,"protein_g":0,"fat_g":0,"carbs_g":0,'
            '"approximate":true}],"questions":[],"not_food":false}.'
        )
        result_schema = {
            "type": "json_schema",
            "name": "nutrition_estimate",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string"},
                                "weight_g": {"type": ["number", "null"]},
                                "portion_text": {"type": "string"},
                                "calories": {"type": "number"},
                                "protein_g": {"type": "number"},
                                "fat_g": {"type": "number"},
                                "carbs_g": {"type": "number"},
                                "approximate": {"type": "boolean"},
                            },
                            "required": [
                                "name", "weight_g", "portion_text", "calories",
                                "protein_g", "fat_g", "carbs_g", "approximate",
                            ],
                        },
                    },
                    "questions": {"type": "array", "items": {"type": "string"}},
                    "not_food": {"type": "boolean"},
                },
                "required": ["items", "questions", "not_food"],
            },
        }
        try:
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": schema_instruction}],
                    },
                    {"role": "user", "content": content},
                ],
                max_output_tokens=1200,
                store=False,
                text={"format": result_schema},
            )
        except Exception as exc:
            raise NutritionAIUnavailable("AI-анализ временно не выполнен") from exc

        text = getattr(response, "output_text", None) or self._extract_output_text(response)
        if not text:
            raise NutritionAIResponseError("AI не вернул описание еды")
        return self._parse_result(text)

    @staticmethod
    def _extract_output_text(response: Any) -> str:
        if hasattr(response, "model_dump"):
            payload = response.model_dump()
        elif isinstance(response, dict):
            payload = response
        else:
            return ""
        chunks: list[str] = []
        for output in payload.get("output", []):
            for item in output.get("content", []):
                if isinstance(item, dict) and item.get("type") in {"output_text", "text"}:
                    chunks.append(str(item.get("text", "")))
        return "\n".join(chunks)

    @staticmethod
    def _parse_result(text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
        try:
            payload = json.loads(cleaned.strip())
        except json.JSONDecodeError as exc:
            raise NutritionAIResponseError("AI вернул некорректный формат") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise NutritionAIResponseError("AI не вернул список блюд")
        if payload.get("not_food") is True:
            raise NutritionAIResponseError("На изображении не удалось определить еду")
        if "not_food" in payload and not isinstance(payload["not_food"], bool):
            raise NutritionAIResponseError("Некорректный признак содержимого изображения")
        raw_questions = payload.get("questions", [])
        if not isinstance(raw_questions, list) or any(not isinstance(value, str) for value in raw_questions):
            raise NutritionAIResponseError("Некорректный список уточняющих вопросов")
        items = []
        for raw in payload["items"]:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()[:200]
            if not name:
                continue
            if "weight_g" not in raw:
                raise NutritionAIResponseError(f"Не указана порция для блюда: {name}")
            weight_raw = raw["weight_g"]
            weight = None if weight_raw is None else NutritionAI._required_number(
                weight_raw, "масса", maximum=100000, strictly_positive=True
            )
            portion = str(raw.get("portion_text", "")).strip()[:200]
            if weight is None and not portion:
                raise NutritionAIResponseError(f"Нужна масса или описание порции: {name}")
            required_fields = ("calories", "protein_g", "fat_g", "carbs_g")
            missing = [field for field in required_fields if field not in raw]
            if missing:
                raise NutritionAIResponseError(
                    f"Не хватает КБЖУ для блюда {name}: {', '.join(missing)}"
                )
            item = {
                "name": name,
                "weight_g": weight,
                "portion_text": portion,
                "calories": NutritionAI._required_number(raw["calories"], "калории", maximum=100000),
                "protein_g": NutritionAI._required_number(raw["protein_g"], "белки", maximum=10000),
                "fat_g": NutritionAI._required_number(raw["fat_g"], "жиры", maximum=10000),
                "carbs_g": NutritionAI._required_number(raw["carbs_g"], "углеводы", maximum=10000),
                "approximate": True,
            }
            items.append(item)
        if not items:
            raise NutritionAIResponseError("AI не распознал ни одного блюда")
        questions = [str(value).strip()[:300] for value in raw_questions if str(value).strip()]
        return {"items": items, "questions": questions[:3]}

    @staticmethod
    def _required_number(
        value: Any,
        label: str,
        *,
        maximum: float,
        strictly_positive: bool = False,
    ) -> float:
        if isinstance(value, bool) or value is None:
            raise NutritionAIResponseError(f"Некорректное значение: {label}")
        try:
            result = float(value)
        except (TypeError, ValueError):
            raise NutritionAIResponseError(f"Некорректное значение: {label}")
        if not math.isfinite(result):
            raise NutritionAIResponseError(f"Неконечное значение: {label}")
        if result < 0 or (strictly_positive and result <= 0) or result > maximum:
            raise NutritionAIResponseError(f"Недопустимое значение: {label}")
        return result
