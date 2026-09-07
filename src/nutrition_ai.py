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
from typing import Any


class NutritionAIUnavailable(RuntimeError):
    """AI-анализ не настроен или временно недоступен."""


class NutritionAIResponseError(ValueError):
    """Модель вернула ответ, который нельзя безопасно показать как черновик."""


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
