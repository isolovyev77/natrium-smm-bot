from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.nutrition_help import (
    MAX_QUESTION_CHARS,
    NutritionHelp,
    NutritionHelpContext,
    is_help_question,
    split_help_questions,
)


class FakeResponses:
    def __init__(self, output_text: str | None = None, error: Exception | None = None):
        self.output_text = output_text
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=self.output_text)


class FakeClient:
    def __init__(self, output_text: str | None = None, error: Exception | None = None):
        self.responses = FakeResponses(output_text, error)


def context(role: str = "client", state: str = "menu") -> NutritionHelpContext:
    return NutritionHelpContext(role=role, state=state, screen="Меню дневника")


def test_split_multipart_preserves_order_and_static_fallback_answers_every_part():
    question = (
        "1. Как добавить воду?\n"
        "2. Где посмотреть неделю?\n"
        "3. Как правильно направить комментарий?"
    )
    assert split_help_questions(question) == [
        "Как добавить воду",
        "Где посмотреть неделю",
        "Как правильно направить комментарий",
    ]
    answer = NutritionHelp(api_key="").answer(question, context("trainer"))
    assert answer.count("\n\n") == 2
    assert answer.startswith("1. ")
    assert "2. " in answer and "3. " in answer
    assert "Добавить воду" in answer
    assert "Сегодня" in answer and "Неделя" in answer
    assert "Технический ID вводить не нужно" in answer


def test_question_detection_is_interface_specific_and_does_not_capture_comment_text():
    assert is_help_question("Как правильно направить комментарий?", state="trainer_comment_text")
    assert is_help_question("Какие единицы у нормы и можно ли вернуться назад?", state="norms_value")
    assert is_help_question("Это граммы или проценты?", state="norms_value")
    assert is_help_question("Я запутался, что вводить?", state="reference_grams")
    assert is_help_question("Не понял, что вводить", state="norms_value")
    assert is_help_question("Запутался с форматом", state="reference_grams")
    assert is_help_question("Не понял, что вводить", state="weight_time")
    assert is_help_question("Где посмотреть статистику?", state=None)
    assert is_help_question("Отправь клиенту комментарий про овощи", state=None)
    assert is_help_question("Что вы умеете?", state=None)
    assert is_help_question("Что ты умеешь?", state=None)
    assert is_help_question("Что умеешь?", state=None)
    assert is_help_question("Что можешь?", state=None)
    assert is_help_question("Зачем нужна масса всей порции?", state="manual_item")
    assert is_help_question("Что сюда писать?", state="manual_item")
    assert not is_help_question("Как прошла тренировка?", state="trainer_comment_text")
    assert not is_help_question("Как прошла тренировка?", state="mealctx_note")
    assert not is_help_question("Салат?", state="manual_item")
    assert not is_help_question("Добавьте, пожалуйста, больше овощей", state="trainer_comment_text")
    assert not is_help_question("Творог и яблоко, порция 250 г?", state="wait_manual")


def test_valid_ai_answer_uses_store_false_and_only_sanitized_context():
    payload = json.dumps({
        "answers": [
            {"question_index": 1, "answer": "Откройте меню."},
            {"question_index": 2, "answer": "Нажмите кнопку помощи."},
        ],
    }, ensure_ascii=False)
    client = FakeClient(payload)
    helper = NutritionHelp(client=client, timeout_seconds=3)
    answer = helper.answer(
        "1. Как устроена поддержка для ID 123456789?\n2. Что она может объяснить?",
        context("trainer", "norms_value"),
    )
    assert answer.startswith("1. Откройте")
    call = client.responses.calls[0]
    assert call["store"] is False
    assert call["max_output_tokens"] == 1000
    serialized = json.dumps(call, ensure_ascii=False)
    assert "123456789" not in serialized
    assert "[ID скрыт]" in serialized
    assert "telegram_id" not in serialized.casefold()


def test_ai_request_numbers_every_part_and_constrains_schema_indexes():
    payload = json.dumps({
        "answers": [
            {"question_index": 1, "answer": "Неточный маршрут модели"},
            {"question_index": 2, "answer": "Норма является целью, факт складывается из записей."},
            {"question_index": 3, "answer": "Пустой день сообщает лишь об отсутствии записей."},
        ],
    }, ensure_ascii=False)
    client = FakeClient(payload)
    helper = NutritionHelp(client=client)
    question = (
        "Как тренеру оставить комментарий клиенту? "
        "Чем дневная норма отличается от фактических значений в дневнике? "
        "Почему по пустому дню нельзя понять, ел ли человек?"
    )
    answer = helper.answer(question, context("trainer"))
    assert "💬 Комментарий" in answer
    assert "Норма является целью" in answer
    assert "Пустой день сообщает" in answer
    call = client.responses.calls[0]
    user_text = call["input"][1]["content"][0]["text"]
    assert "1. Как тренеру оставить комментарий клиенту" in user_text
    assert "Проверенная инструкция 1:" in user_text
    assert "«💬 Комментарий»" in user_text
    assert "2. Чем дневная норма отличается от фактических значений в дневнике" in user_text
    assert "Проверенная инструкция 2:" in user_text
    assert "Фактические значения" in user_text
    assert "3. Почему по пустому дню нельзя понять, ел ли человек" in user_text
    assert "Проверенная инструкция 3:" in user_text
    assert "отсутствие подтвержденных записей" in user_text
    answers_schema = call["text"]["format"]["schema"]["properties"]["answers"]
    assert answers_schema["minItems"] == answers_schema["maxItems"] == 3
    assert answers_schema["items"]["properties"]["question_index"]["enum"] == [1, 2, 3]


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        json.dumps({"answers": [{"question_index": 1, "answer": "Ответ"}]}, ensure_ascii=False),
        json.dumps({"answers": [
            {"question_index": 1, "answer": ""},
            {"question_index": 2, "answer": "Ответ"},
        ]}, ensure_ascii=False),
    ],
)
def test_malformed_or_incomplete_ai_answer_uses_useful_static_fallback(output):
    helper = NutritionHelp(client=FakeClient(output))
    answer = helper.answer(
        "Чем нормы отличаются от фактических значений? Почему пустой день не равен нулю?",
        context("trainer"),
    )
    assert "Дневная норма" in answer
    assert "Пустой день" in answer
    assert "попробуйте позже" not in answer.casefold()


def test_provider_timeout_uses_static_fallback_without_error_text():
    helper = NutritionHelp(client=FakeClient(error=TimeoutError("secret transport detail")))
    answer = helper.answer("Почему в статистике воды нет значения?", context())
    assert "Добавить воду" in answer
    assert "secret" not in answer
    assert "transport" not in answer


def test_voluntary_question_is_sent_without_database_context():
    client = FakeClient(json.dumps({
        "answers": [{"question_index": 1, "answer": "Откройте неделю"}],
    }))
    helper = NutritionHelp(client=client)
    answer = helper.answer("Почему интерфейс показывает это иначе?", context())
    assert answer == "Откройте неделю"
    serialized = json.dumps(client.responses.calls[0], ensure_ascii=False)
    assert "Меню дневника" in serialized
    assert "telegram_id" not in serialized.casefold()


def test_nutrition_units_remain_in_current_question_for_ai():
    client = FakeClient(json.dumps({
        "answers": [{"question_index": 1, "answer": "Используйте исправление"}],
    }))
    helper = NutritionHelp(client=client)
    helper.answer("Почему подсказка про 250 г выглядит иначе?", context("trainer"))
    serialized = json.dumps(client.responses.calls[0], ensure_ascii=False)
    assert "250 г" in serialized


def test_known_routes_skip_provider_and_use_exact_local_instructions():
    client = FakeClient(error=AssertionError("provider must not be called"))
    helper = NutritionHelp(client=client)
    answer = helper.answer(
        "Как оставить комментарий? Где выбрать прием? Как исправить нормы?",
        context("trainer"),
    )
    assert client.responses.calls == []
    assert "«💬 Комментарий»" in answer
    assert "подтвержденные приемы" in answer
    assert "«🎯 Задать нормы»" in answer
    general = helper.answer("Что умеешь?", context("client"))
    assert client.responses.calls == []
    assert "добавление приема пищи и воды" in general


def test_question_limits_are_enforced():
    with pytest.raises(ValueError, match="короче"):
        split_help_questions("x" * (MAX_QUESTION_CHARS + 1))
    with pytest.raises(ValueError, match="Напишите вопрос"):
        split_help_questions("  ")
    with pytest.raises(ValueError, match="Разделите запрос"):
        split_help_questions("? ".join(f"Вопрос {index}" for index in range(9)) + "?")


def test_invalid_timeout_configuration_falls_back_to_bounded_default():
    assert NutritionHelp(api_key="", timeout_seconds=float("nan")).timeout_seconds == 12
    assert NutritionHelp(api_key="", timeout_seconds=-1).timeout_seconds == 12
    assert NutritionHelp(api_key="", timeout_seconds=99).timeout_seconds == 30


def test_real_trainer_multipart_question_gets_three_actionable_answers():
    question = (
        "Как тренеру добавить комментарий клиенту? "
        "Где выбрать нужный приём? "
        "И как потом исправить нормы?"
    )
    answer = NutritionHelp(api_key="").answer(question, context("trainer"))
    assert answer.startswith("1. ")
    assert "2. " in answer and "3. " in answer
    assert "Комментарий" in answer
    assert "подтвержденные приемы" in answer
    assert "Задать нормы" in answer


def test_norm_units_and_photo_correction_have_complete_static_instructions():
    norms = NutritionHelp(api_key="").answer(
        "Это граммы или проценты? Как вернуться к калориям?",
        NutritionHelpContext(
            role="trainer", state="norms_value", screen="Нормы: Белки, г/сутки"
        ),
    )
    assert "граммах на сутки" in norms
    assert "Проценты" in norms
    assert "Назад" in norms

    photo = NutritionHelp(api_key="").answer(
        "Фото насчитало слишком много. Как заменить расчётом по справочнику "
        "и отменить запись, если ошибся?",
        context("client", "draft"),
    )
    assert "Подобрать в справочнике" in photo
    assert "Отменить" in photo
    assert "не попадет в статистику" in photo


def test_confused_wizard_question_uses_exact_safe_field_metadata():
    answer = NutritionHelp(api_key="").answer(
        "Не понял, что сюда вводить",
        NutritionHelpContext(
            role="trainer", state="norms_value", screen="Нормы: Белки, г/сутки"
        ),
    )
    assert "Белки, г/сутки" in answer
    assert "имя клиента" not in answer.casefold()

    weight = NutritionHelp(api_key="").answer(
        "Не понял, что вводить",
        NutritionHelpContext(
            role="client", state="weight_time",
            screen="Местное время измерения веса, ЧЧ:ММ",
        ),
    )
    assert "Местное время измерения веса, ЧЧ:ММ" in weight
    assert "часовом поясе профиля" in weight

    portion = NutritionHelp(api_key="").answer(
        "Зачем нужна масса всей порции?",
        NutritionHelpContext(
            role="client", state="manual_item",
            screen="Ручной ввод: Масса всей порции, г, например: 180",
        ),
    )
    assert "общий вес всей порции" in portion
    assert "в граммах" in portion


def test_help_never_claims_that_it_sent_a_comment():
    answer = NutritionHelp(api_key="").answer(
        "Отправь клиенту комментарий, что нужно добавить овощи",
        context("trainer", "trainer_comment_text"),
    )
    assert "Откройте" in answer
    assert "отправил" not in answer.casefold()
