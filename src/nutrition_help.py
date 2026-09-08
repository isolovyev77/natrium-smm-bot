"""Контекстная справка по дневнику питания Natrium.

Модуль не читает и не изменяет данные дневника. В AI-запрос передаются только
текст вопроса, роль и название текущего экрана. При недоступном AI используется
полезная встроенная справка по реально существующим кнопкам и сценариям.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


MAX_QUESTION_CHARS = 2000
MAX_ANSWER_CHARS = 3500
MAX_QUESTION_PARTS = 12
MAX_HISTORY_TURNS = 3
MAX_HISTORY_CHARS = 3000


class NutritionHelpResponseError(ValueError):
    """AI вернул ответ, который нельзя безопасно показать пользователю."""


@dataclass(frozen=True)
class NutritionHelpContext:
    role: str
    state: str
    screen: str

    def __post_init__(self) -> None:
        if self.role not in {"client", "trainer"}:
            raise ValueError("Неизвестная роль для справки")


def split_help_questions(text: str) -> list[str]:
    """Делит многочастный вопрос, сохраняя порядок пользовательских пунктов."""
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    if not normalized:
        raise ValueError("Напишите вопрос о работе дневника")
    if len(normalized) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"Сообщение длиннее допустимых {MAX_QUESTION_CHARS} символов. "
            "Разделите запрос на несколько сообщений."
        )

    raw_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    numbered = len(raw_lines) > 1 and any(
        re.match(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+", line)
        for line in normalized.splitlines()
    )
    if numbered:
        parts: list[str] = []
        current: list[str] = []
        preamble: list[str] = []
        for line in raw_lines:
            marker = re.match(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+", line)
            if marker:
                if current:
                    parts.append(" ".join(current))
                current = [line[marker.end():].strip()]
            elif current:
                current.append(line)
            else:
                preamble.append(line)
        if current:
            parts.append(" ".join(current))
        if preamble and parts:
            parts[0] = " ".join([*preamble, parts[0]])
    else:
        parts = [part.strip() for part in re.findall(r"[^?？]+(?:[?？]|$)", normalized)]
    parts = [part.rstrip("?？. ").strip() for part in parts if part.strip()]
    if not parts:
        raise ValueError("Напишите вопрос о работе дневника")
    if len(parts) > MAX_QUESTION_PARTS:
        raise ValueError(
            f"В одном сообщении можно разобрать до {MAX_QUESTION_PARTS} вопросов. "
            "Разделите запрос на несколько сообщений."
        )
    return parts


_INTERFACE_WORDS = (
    "бот", "дневник", "кноп", "меню", "экран", "вернут", "назад", "отмен",
    "добав", "ввест", "загруз", "отправ", "сохран", "подтверд", "посмотр",
    "комментар", "норм", "xlsx", "код тренер", "привяз", "фото", "справочник",
    "usda", "вода", "часов", "клиент", "тренер", "запис", "прием пищи",
)
_QUESTION_STARTS = (
    "как ", "где ", "куда ", "что ", "зачем ", "почему ", "можно ли ",
    "каким образом ", "подскажите", "помогите", "объясните",
)
_COMMENT_HELP_PATTERNS = (
    r"как\s+.*(?:добав|остав|направ|отправ).*комментар",
    r"куда\s+.*комментар",
    r"где\s+.*комментар",
    r"комментар.*(?:кноп|бот|дневник|клиент|прием|запис)",
)
_GENERAL_HELP_PATTERNS = (
    r"^что\s+(?:(?:вы|ты|бот)\s+)?уме(?:ете|ешь|ет)",
    r"^что\s+(?:(?:вы|ты|бот)\s+)?мож(?:ете|ешь|ет)",
    r"^чем\s+(?:вы|ты|бот)\s+(?:может|можешь)",
    r"^как\s+(?:вы|ты|бот)\s+(?:может|можешь).*помоч",
    r"^(?:что|какие)\s+(?:здесь\s+)?(?:можно|доступно|функци)",
    r"^как\s+(?:этим|дневником|ботом)\s+пользоват",
)


def is_help_question(text: str, *, state: str | None = None) -> bool:
    """Распознает явный вопрос об интерфейсе, не перехватывая обычный комментарий."""
    value = " ".join(text.casefold().replace("ё", "е").split())
    if not value or len(value) > MAX_QUESTION_CHARS:
        return False
    if re.search(r"^(?:отправь|добавь|оставь)\s+.*комментар", value):
        return True
    has_question_form = "?" in value or any(value.startswith(item) for item in _QUESTION_STARTS)
    if state == "trainer_comment_text":
        if not has_question_form:
            return False
        return "бот" in value or "кноп" in value or "меню" in value or any(
            re.search(pattern, value) for pattern in _COMMENT_HELP_PATTERNS
        )
    if state in {None, "menu"} and has_question_form and _is_general_help_intent(value):
        return True
    wizard_states = {
        "reference_search", "reference_grams", "reference_reweight", "wait_photo",
        "wait_manual", "manual_only", "clarify_ai", "edit_draft", "wait_water",
        "wait_link_code", "wait_timezone", "norms_date", "norms_date_other",
        "norms_value", "norms_preview", "trainer_plan_upload", "trainer_plan_preview",
        "trainer_edit_items", "manual_item", "manual_preview", "profile_value",
        "profile_preview", "weight_value", "weight_date", "weight_date_other",
        "weight_time", "weight_note", "weight_preview",
        "weight_edit_value", "weight_history_date", "mealctx_type", "mealctx_date",
        "mealctx_date_other", "mealctx_time", "mealctx_note", "mealctx_hunger",
        "mealctx_mood", "mealctx_preview", "clientrem_meal", "clientrem_meal_times",
        "clientrem_water", "clientrem_water_value", "clientrem_water_range",
        "clientrem_quiet", "clientrem_preview", "trainerrem_time", "trainerrem_days",
        "trainerrem_compare", "trainerrem_percent", "trainerrem_preview",
        "draft_correction_target", "draft_correction_text", "edit_draft_field",
    }
    wizard_help_words = (
        "грамм", "процент", "единиц", "формат", "дат", "сколько", "не понял",
        "не понимаю", "запутал", "что ввод", "что указ", "что сюда пис",
        "что в этом поле", "что означает поле",
    )
    if state in wizard_states and any(word in value for word in wizard_help_words):
        return True
    wizard_fields = (
        "масса", "калори", "белк", "жир", "углевод", "рост", "цель",
        "часовой пояс", "дата", "время", "интервал", "тихие часы",
        "голод", "настроение",
    )
    if (
        state in wizard_states
        and value.startswith(("зачем ", "почему "))
        and any(field in value for field in wizard_fields)
    ):
        return True
    if not has_question_form:
        return False
    return any(word in value for word in _INTERFACE_WORDS)


def _is_general_help_intent(value: str) -> bool:
    return any(re.search(pattern, value) for pattern in _GENERAL_HELP_PATTERNS)


def sanitize_help_text(text: str) -> str:
    """Удаляет из текста справки секреты, идентификаторы и детали рациона."""
    safe = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[данные скрыты]", text)
    safe = re.sub(r"\b\d{6,}\b", "[ID скрыт]", safe)
    safe = re.sub(
        r"(?i)\b(?:token|пароль|password|secret|api[_ -]?key|код)\s*[:=]?\s*[A-Za-z0-9_\-]{6,}",
        "[секрет скрыт]",
        safe,
    )
    safe = re.sub(
        r"(?im)\b(?:(?:мой|моя|свой|этот)\s+)?рацион\s*[:=\-]\s*[^\n]+",
        "[рацион скрыт]",
        safe,
    )
    safe = re.sub(
        r"(?im)\b(?:я\s+(?:ел|ела|съел|съела)|что\s+я\s+ел(?:а)?)\s*[:=\-]\s*[^\n]+",
        "[рацион скрыт]",
        safe,
    )
    return safe


def _safe_question_for_ai(text: str) -> str:
    """Обратимо сохраняет прежнюю внутреннюю точку вызова sanitizer."""
    return sanitize_help_text(text)


def _safe_history(
    history: Sequence[Mapping[str, str]] | None,
) -> list[dict[str, str]]:
    """Оставляет несколько последних очищенных пар в ограниченном бюджете."""
    if not history:
        return []
    result: list[dict[str, str]] = []
    used = 0
    for turn in reversed(list(history)[-MAX_HISTORY_TURNS:]):
        question = sanitize_help_text(str(turn.get("question", ""))).strip()[:900]
        answer = sanitize_help_text(str(turn.get("answer", ""))).strip()[:1200]
        if not question or not answer:
            continue
        remaining = MAX_HISTORY_CHARS - used
        if remaining <= 0:
            break
        if len(question) + len(answer) > remaining:
            answer = answer[: max(0, remaining - len(question))].rstrip()
        if not answer:
            break
        result.append({"question": question, "answer": answer})
        used += len(question) + len(answer)
    result.reverse()
    return result


class NutritionHelp:
    """Ответы по функциям бота с AI-улучшением и статическим резервом."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("NUTRITION_HELP_MODEL", "gpt-4o-mini")
        timeout_raw = (
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("NUTRITION_HELP_TIMEOUT_SECONDS", "12")
        )
        try:
            parsed_timeout = float(timeout_raw)
        except (TypeError, ValueError):
            parsed_timeout = 12.0
        if not math.isfinite(parsed_timeout) or parsed_timeout <= 0:
            parsed_timeout = 12.0
        self.timeout_seconds = min(parsed_timeout, 30.0)
        self.client = client
        if self.client is None and self.api_key:
            try:
                from openai import OpenAI
            except ImportError:
                self.client = None
            else:
                self.client = OpenAI(
                    api_key=self.api_key,
                    timeout=self.timeout_seconds,
                    max_retries=0,
                )

    @property
    def available(self) -> bool:
        return self.client is not None

    def answer(
        self,
        question: str,
        context: NutritionHelpContext,
        *,
        history: Sequence[Mapping[str, str]] | None = None,
    ) -> str:
        safe_question = sanitize_help_text(question)
        parts = split_help_questions(safe_question)
        fallback = self._static_answer(parts, context)
        known_routes = [self._known_route_answer(part, context) for part in parts]
        if all(route is not None for route in known_routes):
            return self._format_answers([str(route) for route in known_routes])
        if not self.available:
            return fallback
        try:
            return self._request(
                safe_question,
                parts,
                context,
                history=_safe_history(history),
            )
        except Exception:
            return fallback

    def fallback_answer(self, question: str, context: NutritionHelpContext) -> str:
        """Возвращает локальную справку без обращения к внешнему провайдеру."""
        return self._static_answer(split_help_questions(question), context)

    def _request(
        self,
        question: str,
        parts: list[str],
        context: NutritionHelpContext,
        *,
        history: Sequence[Mapping[str, str]] = (),
    ) -> str:
        question_indexes = list(range(1, len(parts) + 1))
        schema = {
            "type": "json_schema",
            "name": "nutrition_help_answer",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "answers": {
                        "type": "array",
                        "minItems": len(parts),
                        "maxItems": len(parts),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "question_index": {
                                    "type": "integer",
                                    "enum": question_indexes,
                                },
                                "answer": {"type": "string"},
                            },
                            "required": ["question_index", "answer"],
                        },
                    },
                },
                "required": ["answers"],
            },
        }
        knowledge = self._knowledge(context.role)
        known_routes = [self._known_route_answer(part, context) for part in parts]
        groundings = [
            route if route is not None else self._static_answer_one(part, context)
            for part, route in zip(parts, known_routes)
        ]
        numbered_questions = "\n".join(
            f"{index}. {_safe_question_for_ai(part) or '[вопрос скрыт]'}\n"
            f"Проверенная инструкция {index}: {groundings[index - 1]}"
            for index, part in enumerate(parts, 1)
        )
        system = (
            "Ты справка по Telegram-дневнику питания Natrium. Отвечай только по базе функций "
            "ниже. Не назначай нормы и лечение, не додумывай кнопки. Верни отдельный короткий "
            "ровно один answer для каждого пронумерованного вопроса. В question_index укажи "
            "его номер от 1 до числа вопросов. Сохрани исходный порядок и не объединяй пункты. "
            "Под каждым вопросом дана проверенная локальная инструкция. Можно сделать ее понятнее, "
            "но нельзя менять порядок действий, названия кнопок, роль или место действия. "
            "Если не можешь сохранить маршрут точно, повтори проверенную инструкцию дословно. "
            "Если проверенной инструкции недостаточно для точного ответа на вопрос, честно скажи, "
            "что точного подтвержденного ответа нет, и задай один конкретный вопрос о текущем "
            "экране или действии пользователя. Не подменяй такой ответ общей справкой. "
            "Не проси Telegram ID, рацион, "
            "фото, пароль или код. Не выполняй действий и не утверждай, что отправил сообщение "
            "или изменил данные: только объясняй следующий шаг в интерфейсе.\n\n"
            f"Роль: {context.role}. Экран: {context.screen}.\n\n{knowledge}"
        )
        input_messages: list[dict[str, Any]] = [
            {"role": "system", "content": [{"type": "input_text", "text": system}]}
        ]
        for turn in _safe_history(history):
            input_messages.extend([
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": turn["question"]}],
                },
                {
                    "role": "assistant",
                    "content": turn["answer"],
                },
            ])
        input_messages.append(
            {"role": "user", "content": [{"type": "input_text", "text": numbered_questions}]}
        )
        response = self.client.responses.create(
            model=self.model,
            input=input_messages,
            max_output_tokens=1000,
            store=False,
            text={"format": schema},
        )
        raw = getattr(response, "output_text", "")
        if not raw:
            raise NutritionHelpResponseError("Пустой ответ справки")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NutritionHelpResponseError("Некорректный ответ справки") from exc
        if not isinstance(payload, dict):
            raise NutritionHelpResponseError("Некорректный ответ справки")
        answers = payload.get("answers")
        if not isinstance(answers, list) or len(answers) != len(parts):
            raise NutritionHelpResponseError("Ответ охватывает не все вопросы")
        by_index: dict[int, str] = {}
        for item in answers:
            if not isinstance(item, dict) or set(item) != {"question_index", "answer"}:
                raise NutritionHelpResponseError("Некорректный список ответов")
            index = item["question_index"]
            answer = item["answer"]
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index in by_index
                or not isinstance(answer, str)
                or not answer.strip()
            ):
                raise NutritionHelpResponseError("Некорректный текст справки")
            by_index[index] = answer.strip()
        if set(by_index) != set(range(1, len(parts) + 1)):
            raise NutritionHelpResponseError("Ответ охватывает не все вопросы")
        ordered = [
            known_routes[index - 1] or by_index[index]
            for index in range(1, len(parts) + 1)
        ]
        result = self._format_answers(ordered)
        if len(result) > MAX_ANSWER_CHARS:
            raise NutritionHelpResponseError("Ответ справки слишком длинный")
        return result

    @staticmethod
    def _format_answers(answers: list[str]) -> str:
        if len(answers) == 1:
            return answers[0]
        return "\n\n".join(
            f"{index}. {answer}" for index, answer in enumerate(answers, 1)
        )

    @classmethod
    def _known_route_answer(
        cls, question: str, context: NutritionHelpContext
    ) -> str | None:
        """Возвращает только однозначный маршрут по существующим кнопкам."""
        value = question.casefold().replace("ё", "е")
        if context.state in {"menu", ""} and _is_general_help_intent(value):
            return cls._generic_answer(context)
        asks_how = value.startswith(("как ", "где ", "куда ")) or "кнопк" in value
        action = asks_how and any(
            stem in value
            for stem in (
                "добав", "остав", "отправ", "направ", "выб", "зад", "измен",
                "исправ", "загруз", "ввест", "запис", "посмотр", "открыт",
                "вернут", "отмен", "наж",
            )
        )
        if "комментар" in value and action:
            return cls._static_answer_one(question, context)
        if context.role == "trainer" and "выб" in value and "прием" in value:
            return cls._static_answer_one(question, context)
        if "норм" in value and action:
            return cls._static_answer_one(question, context)
        if "xlsx" in value and action:
            return cls._static_answer_one(question, context)
        if any(topic in value for topic in ("код", "привяз", "справочник", "usda")) and action:
            return cls._static_answer_one(question, context)
        if any(topic in value for topic in ("вода", "профил", "вес", "кабинет", "браузер", "напомин", "сводк")) and action:
            return cls._static_answer_one(question, context)
        if any(topic in value for topic in ("голод", "настроен", "контекст")) and action:
            return cls._static_answer_one(question, context)
        if any(topic in value for topic in ("недел", "сегодня", "статист")) and action:
            return cls._static_answer_one(question, context)
        if any(topic in value for topic in ("часовой пояс", "timezone")) and action:
            return cls._static_answer_one(question, context)
        if any(topic in value for topic in ("назад", "вернут", "отмен")) and action:
            return cls._static_answer_one(question, context)
        if any(topic in value for topic in ("исправ", "редакт")) and action:
            return cls._static_answer_one(question, context)
        return None

    @classmethod
    def _static_answer(cls, parts: list[str], context: NutritionHelpContext) -> str:
        answers = [cls._static_answer_one(part, context) for part in parts]
        if len(parts) == 1:
            return answers[0]
        return "\n\n".join(f"{index}. {answer}" for index, answer in enumerate(answers, 1))

    @staticmethod
    def _static_answer_one(question: str, context: NutritionHelpContext) -> str:
        value = question.casefold().replace("ё", "е")
        if "норм" in value and any(word in value for word in ("фактич", "итого", "отлич")):
            return (
                "Дневная норма задает целевое значение с выбранной даты. Фактические значения "
                "суммируются только из подтвержденных записей еды и воды. Бот не считает "
                "отсутствие записи нулевым потреблением."
            )
        if "пуст" in value and any(word in value for word in ("ел", "еда", "прием", "нул")):
            return (
                "Пустой день означает только отсутствие подтвержденных записей. По нему нельзя "
                "заключить, что человек не ел: прием мог быть не внесен в дневник."
            )
        if context.state == "manual_item":
            field = context.screen.removeprefix("Ручной ввод: ")
            if field.startswith("Масса всей порции"):
                return (
                    f"Сейчас поле «{field}». Укажите общий вес всей порции, для которой "
                    "далее вводите калории и БЖУ. Значение вводится в граммах, например 180. "
                    "«⬅️ Назад» сохраняет название продукта."
                )
            return (
                f"Сейчас поле «{field}». "
                "Введите только это значение. «⬅️ Назад» вернет к предыдущему полю, "
                "«Отмена» выйдет без создания черновика."
            )
        if context.state == "profile_value":
            return (
                f"Сейчас поле «{context.screen.removeprefix('Профиль: ')}». "
                "Можно ввести новое значение, оставить текущее или не указывать необязательное поле."
            )
        if context.state == "draft_correction_target":
            return (
                "Выберите одно блюдо, которое нужно заменить. Остальные позиции черновика "
                "останутся без изменений. После исправления проверьте новый расчет и снова "
                "подтвердите черновик."
            )
        if context.state == "draft_correction_text":
            return (
                "Опишите выбранное блюдо обычной фразой: укажите правильное название и, "
                "если известно, массу. ИИ создаст новую приблизительную оценку. Проверьте "
                "КБЖУ и снова подтвердите черновик."
            )
        if context.state == "edit_draft_field":
            return (
                f"Сейчас поле «{context.screen.removeprefix('Исправление блюда: ')}». "
                "Введите одно значение в единицах из подсказки. Для массы бот пересчитает "
                "КБЖУ порции пропорционально; «⬅️ Назад» вернет к выбору поля."
            )
        if context.state in {
            "weight_value", "weight_date", "weight_date_other", "weight_time",
            "weight_note", "weight_preview", "weight_edit_value",
        }:
            return (
                f"Сейчас шаг «{context.screen}». "
                "Вес вводится в килограммах, дата и время считаются в часовом поясе профиля. "
                "«⬅️ Назад» сохраняет уже введенные значения, запись появится только после подтверждения."
            )
        if context.state == "weight_history_date":
            return "Введите дату в формате ГГГГ-ММ-ДД, например 2026-09-08. Затем выберите запись для исправления."
        if context.state.startswith("mealctx_"):
            return (
                f"Сейчас шаг «{context.screen}». Поля голода и настроения необязательны. "
                "«⬅️ Назад» сохраняет уже выбранный контекст, запись меняется только после подтверждения."
            )
        if context.state.startswith(("clientrem_", "trainerrem_")):
            return (
                f"Сейчас шаг «{context.screen}». Время задается в часовом поясе профиля. "
                "Напоминания включатся только после итогового подтверждения; «Отмена» не сохраняет изменения."
            )
        if context.state == "norms_value" and any(
            word in value for word in ("не понял", "не понимаю", "запутал", "что ввод", "что указ")
        ):
            return (
                f"Сейчас поле «{context.screen.removeprefix('Нормы: ')}». "
                "Введите дневное значение в указанной единице или выберите одну из кнопок под сообщением."
            )
        if context.state in {"norms_value", "norms_preview", "norms_date", "norms_date_other"} and (
            "грам" in value or "процент" in value
        ):
            return (
                "Белки, жиры и углеводы вводятся в граммах на сутки, калории в ккал/сутки, "
                "вода в мл/сутки. Проценты мастер не принимает. «⬅️ Назад» возвращает к "
                "предыдущему полю и сохраняет введенные значения."
            )
        if "фото" in value and "справочник" in value:
            return (
                "В черновике после фото нажмите «📚 Подобрать в справочнике», выберите продукт "
                "USDA и укажите массу. Проверьте новый расчет. Если запись не нужна, нажмите "
                "«Отменить»: неподтвержденный черновик не попадет в статистику."
            )
        if "комментар" in value:
            if context.role == "trainer":
                return (
                    "Откройте «👥 Кабинет тренера», выберите клиента, нажмите «💬 Комментарий», "
                    "выберите прием по названию, дате и времени, затем отправьте текст. "
                    "Технический ID вводить не нужно."
                )
            return "Комментарий к подтвержденному приему добавляет привязанный тренер в своем кабинете."
        if context.role == "trainer" and "выб" in value and "прием" in value:
            return (
                "После кнопки «💬 Комментарий» бот показывает подтвержденные приемы с названием, "
                "датой и временем. Выберите нужную строку, затем отправьте текст комментария."
            )
        if "xlsx" in value or ("план" in value and "норм" in value):
            if context.role != "trainer":
                return "План XLSX загружает тренер из карточки привязанного клиента."
            return (
                "В карточке клиента нажмите «📄 Скачать шаблон», заполните лист «План», затем "
                "нажмите «📎 Загрузить план XLSX». Проверьте preview и только потом примените план. "
                "Совпадающие даты заменяются, другие даты сохраняются."
            )
        if "норм" in value or "ккал/сут" in value:
            if context.role != "trainer":
                return "Дневные нормы задает привязанный тренер. Бот сам нормы не назначает."
            return (
                "В карточке клиента нажмите «🎯 Задать нормы». Выберите дату начала, затем укажите "
                "ккал/сутки, белки, жиры и углеводы в г/сутки, воду в мл/сутки. Перед записью "
                "проверьте preview; «⬅️ Назад» сохраняет уже введенные значения."
            )
        if "код" in value or "привяз" in value:
            if context.role == "trainer":
                return (
                    "В «👥 Кабинете тренера» нажмите «🔑 Код для клиентов». Код действует до "
                    "замены. Замена закрывает старый код для новых подключений и сохраняет текущих клиентов."
                )
            return (
                "В дневнике откройте «⚙️ Настройки», нажмите «🔗 Ввести код тренера» "
                "и введите код, который дал тренер. На первом экране также есть кнопка "
                "«🔗 У меня есть код тренера»."
            )
        if "usda" in value or "справочник" in value or "точн" in value:
            return (
                "Нажмите «🍽️ Добавить прием пищи» и «📚 Рассчитать по справочнику». Найдите "
                "подходящий продукт и способ приготовления, выберите запись USDA и укажите массу. "
                "Проверяйте соответствие продукта: похожие названия не всегда означают одинаковый состав."
            )
        if "фото" in value or "openai" in value:
            return (
                "Фото передается искусственному интеллекту только после согласия и создает "
                "приблизительный черновик. "
                "Проверьте блюдо и порцию, затем исправьте, подтвердите или отмените запись. "
                "Согласие можно отозвать через «⚙️ Настройки»."
            )
        if "вод" in value:
            return "В меню дневника нажмите «💧 Добавить воду», выберите 250/500 мл или введите другой объем."
        if "профил" in value or "рост" in value or "цель" in value:
            return (
                "В меню дневника откройте «⚙️ Настройки», затем «👤 Профиль» и "
                "нажмите «✏️ Изменить профиль». "
                "Мастер по очереди спросит имя, рост в см, цель и часовой пояс IANA. "
                "Изменения записываются только после проверки и подтверждения."
            )
        if "вес" in value:
            return (
                "Откройте «⚖️ Вес». Там можно записать вес в кг, посмотреть историю за 14 дней "
                "и открыть конкретную дату для исправления или отмены записи."
            )
        if "голод" in value or "настроен" in value or "контекст" in value:
            return (
                "В черновике приема нажмите «📝 Контекст приема». Укажите тип, местные дату и "
                "время, а при желании заметку, голод от 1 до 10 и настроение. Необязательные "
                "поля можно пропустить."
            )
        if "кабинет" in value or "браузер" in value:
            return (
                "Кнопка «🌐 Веб-кабинет» создает одноразовую ссылку на 10 минут. "
                "Ссылка открывает кабинет текущего пользователя; права проверяются снова на сервере."
            )
        if "недел" in value or "сегодня" in value or "статист" in value:
            return (
                "Кнопки «📊 Сегодня» и «📅 Неделя» считают только подтвержденные записи. "
                "Пустой день означает отсутствие записей, а не отсутствие еды."
            )
        if "час" in value or "timezone" in value:
            return (
                "Откройте «⚙️ Настройки», нажмите «🕒 Часовой пояс» и введите "
                "название IANA, например Europe/Moscow."
            )
        if "напомин" in value or "сводк" in value:
            if context.role == "trainer" and "сводк" in value:
                return (
                    "В «👥 Кабинете тренера» откройте «🔔 Ежедневная сводка». Настройте местное "
                    "время, порог дней без записей и необязательное сравнение с нормой."
                )
            return (
                "В дневнике откройте «⚙️ Настройки», затем «⏰ Напоминания». "
                "Отдельно настройте время еды, воду и "
                "тихие часы. Это просьба внести запись, а не утверждение, что приема пищи не было."
            )
        if "назад" in value or "вернут" in value or "отмен" in value:
            return (
                "«⬅️ Назад» возвращает на предыдущий экран и сохраняет ввод мастера норм. "
                "«Отмена» выходит из текущего действия без записи неподтвержденных изменений."
            )
        if "исправ" in value or "редакт" in value:
            if context.role == "trainer":
                return (
                    "Откройте клиента, нажмите «✏️ Исправить прием» и выберите запись по названию, "
                    "дате и времени. Доступ есть только к привязанным клиентам."
                )
            return (
                "В черновике нажмите «✨ Исправить словами», чтобы обычной фразой заменить "
                "выбранное блюдо, или «✏️ Исправить поле», чтобы изменить название, массу "
                "или отдельное значение КБЖУ. Для полностью автономного ввода выберите "
                "«⌨️ Заменить все полным вводом»: можно вставить скопированный черновик, "
                "шесть строк название/масса/ккал/Б/Ж/У или прежнюю строку с точками с запятой. "
                "После любого исправления проверьте и снова подтвердите черновик."
            )
        return NutritionHelp._unknown_answer()

    @staticmethod
    def _generic_answer(context: NutritionHelpContext) -> str:
        return (
            "В дневнике доступны добавление приема пищи и воды, вес, сводки «Сегодня» и "
            "«Неделя», повтор приема и комментарии тренера. Профиль, напоминания, "
            "привязка тренера, часовой пояс и отзыв согласия находятся в «⚙️ Настройки». "
            + (
                "В кабинете тренера также доступны нормы, XLSX-план, исправление и комментарий."
                if context.role == "trainer"
                else "Напишите, про какую кнопку или шаг нужно объяснение."
            )
        )

    @staticmethod
    def _unknown_answer() -> str:
        return (
            "Точного подтвержденного ответа по этому вопросу у меня сейчас нет. "
            "На каком экране вы находитесь и какую кнопку или действие хотите выполнить?"
        )

    @staticmethod
    def _knowledge(role: str) -> str:
        common = (
            "Функции клиента: «🍽️ Добавить прием пищи» с вариантами USDA, фото через ИИ, "
            "описание с помощью ИИ и ручной КБЖУ; исправление черновика словами, по одному "
            "полю или полным автономным вводом; «💧 Добавить воду»; «📊 Сегодня»; "
            "«📅 Неделя»; повтор подтвержденного приема; последние комментарии тренера; "
            "«⚖️ Вес» с историей и исправлением. В «⚙️ Настройки» находятся код тренера, "
            "часовой пояс, отзыв согласия, профиль и напоминания. Контекст приема включает тип, "
            "местными датой и временем, заметкой, необязательными голодом и настроением; "
            "одноразовый вход в веб-кабинет; отключение тренера. Фото через ИИ всегда дает "
            "приблизительный черновик. USDA требует выбора "
            "продукта и массы. В статистику входят только подтвержденные записи."
        )
        if role == "trainer":
            return common + (
                " Функции тренера: «👥 Кабинет тренера», постоянный код до замены, только "
                "привязанные клиенты, нормы с датой начала, XLSX после preview, исправление приема "
                "и комментарий после выбора записи. Бот не назначает медицинские нормы."
            )
        return common + " Нормы и комментарии задает привязанный тренер."
