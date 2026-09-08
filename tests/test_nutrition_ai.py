import json
from types import SimpleNamespace

import pytest

from src.nutrition_ai import (
    NutritionAI,
    NutritionAIIntentAmbiguous,
    NutritionAIResponseError,
    NutritionAITargetAmbiguous,
    NutritionAIUnavailable,
)


def result_json(**overrides):
    payload = {
        "items": [
            {
                "name": "Гречка",
                "weight_g": 180,
                "portion_text": "одна тарелка",
                "calories": 190,
                "protein_g": 6,
                "fat_g": 2,
                "carbs_g": 38,
                "approximate": True,
            }
        ],
        "questions": [],
        "not_food": False,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class FakeResponses:
    def __init__(self, output_text=None, error=None):
        self.output_text = output_text
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=self.output_text)


class SequencedResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(output_text=outcome)


def ai_with_response(output_text=None, error=None):
    responses = FakeResponses(output_text=output_text, error=error)
    return NutritionAI(client=SimpleNamespace(responses=responses), model="vision-test"), responses


def test_response_request_uses_strict_schema_and_never_stores_content():
    ai, responses = ai_with_response(result_json())
    result = ai.analyze_text("гречка, одна тарелка")

    assert result["items"][0]["name"] == "Гречка"
    call = responses.calls[0]
    assert call["model"] == "vision-test"
    assert call["store"] is False
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True
    schema = call["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False


def test_photo_is_sent_as_data_url_and_result_remains_approximate():
    ai, responses = ai_with_response(
        result_json(items=[{
            "name": "Омлет",
            "weight_g": 150,
            "portion_text": "порция",
            "calories": 240,
            "protein_g": 18,
            "fat_g": 16,
            "carbs_g": 4,
            "approximate": False,
        }])
    )
    result = ai.analyze_photo(b"image-bytes", "image/png")

    content = responses.calls[0]["input"][1]["content"]
    image = next(item for item in content if item["type"] == "input_image")
    assert image["image_url"].startswith("data:image/png;base64,")
    assert result["items"][0]["approximate"] is True
    assert "confidence" not in result["items"][0]


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        json.dumps({"items": "wrong", "questions": [], "not_food": False}),
        json.dumps({"items": ["wrong"], "questions": [], "not_food": False}),
        result_json(questions="Уточните порцию"),
        result_json(not_food="false"),
    ],
)
def test_malformed_results_are_rejected(raw):
    with pytest.raises(NutritionAIResponseError):
        NutritionAI._parse_result(raw)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf"), True, None])
def test_nonfinite_and_non_numeric_macros_are_never_converted_to_zero(bad_value):
    item = json.loads(result_json())["items"][0]
    item["calories"] = bad_value
    with pytest.raises(NutritionAIResponseError):
        NutritionAI._parse_result(result_json(items=[item]))


@pytest.mark.parametrize("missing", ["calories", "protein_g", "fat_g", "carbs_g", "weight_g"])
def test_missing_required_food_data_is_not_zero_filled(missing):
    item = json.loads(result_json())["items"][0]
    item.pop(missing)
    with pytest.raises(NutritionAIResponseError):
        NutritionAI._parse_result(result_json(items=[item]))


def test_no_food_and_empty_items_are_explicit_failures():
    with pytest.raises(NutritionAIResponseError, match="еду"):
        NutritionAI._parse_result(result_json(items=[], not_food=True))
    with pytest.raises(NutritionAIResponseError, match="ни одного"):
        NutritionAI._parse_result(result_json(items=[]))


def test_timeout_or_provider_error_becomes_unavailable_for_manual_fallback():
    ai, _ = ai_with_response(error=TimeoutError("provider timeout and secret details"))
    with pytest.raises(NutritionAIUnavailable) as caught:
        ai.analyze_text("обед")
    assert "secret" not in str(caught.value)
    assert caught.value.__cause__ is not None


def test_missing_client_and_empty_inputs_fail_before_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ai = NutritionAI(api_key=None, client=None)
    assert ai.available is False
    with pytest.raises(NutritionAIUnavailable):
        ai.analyze_text("обед")
    with pytest.raises(NutritionAIUnavailable):
        ai.analyze_photo(b"photo")

    available, responses = ai_with_response(result_json())
    with pytest.raises(ValueError):
        available.analyze_text("   ")
    with pytest.raises(ValueError):
        available.analyze_photo(b"")
    assert responses.calls == []


def test_nested_output_text_is_supported_but_empty_response_is_rejected():
    nested = {
        "output": [
            {"content": [{"type": "output_text", "text": result_json()}]}
        ]
    }
    assert NutritionAI._extract_output_text(nested) == result_json()

    ai, _ = ai_with_response("")
    with pytest.raises(NutritionAIResponseError):
        ai.analyze_text("обед")


def test_exact_user_clarification_replaces_wrong_hypothesis_with_one_composite_item():
    response = result_json(items=[{
        "name": "Гречневые хлопья с семенами и кедровыми орехами",
        "weight_g": 150,
        "portion_text": "150 г",
        "calories": 510,
        "protein_g": 16,
        "fat_g": 19,
        "carbs_g": 68,
        "approximate": False,
        "fdc_id": 999,
        "source": "USDA",
    }])
    ai, responses = ai_with_response(response)
    original = [{
        "name": "Овсянка с ягодами годжи",
        "weight_g": 150,
        "portion_text": "150 г",
        "calories": 240,
        "protein_g": 7,
        "fat_g": 5,
        "carbs_g": 41,
        "approximate": True,
    }]

    result = ai.apply_clarification(
        "Это гречневые хлопья с семенами и кедровыми орехами 294г",
        original,
    )

    assert result["operation"] == "replace"
    assert result["target_index"] == 0
    assert len(result["items"]) == 1
    corrected = result["items"][0]
    assert corrected["name"] == "Гречневые хлопья с семенами и кедровыми орехами"
    assert corrected["weight_g"] == 294
    assert corrected["portion_text"] == "294 г"
    assert corrected["calories"] == pytest.approx(999.6)
    assert corrected["protein_g"] == pytest.approx(31.36)
    assert corrected["fat_g"] == pytest.approx(37.24)
    assert corrected["carbs_g"] == pytest.approx(133.28)
    assert corrected["approximate"] is True
    assert corrected["calculation_method"] == "ai"
    assert "fdc_id" not in corrected
    assert "source" not in corrected
    assert original[0]["name"] == "Овсянка с ягодами годжи"
    prompt = responses.calls[0]["input"][1]["content"][0]["text"]
    assert "Не возвращай исходную гипотезу вторым item" in prompt
    assert "одной составной позицией" in prompt


def test_explicit_replacement_keeps_unselected_draft_items_unchanged():
    ai, _ = ai_with_response(result_json(items=[{
        "name": "Гречневые хлопья",
        "weight_g": 200,
        "portion_text": "200 г",
        "calories": 330,
        "protein_g": 10,
        "fat_g": 5,
        "carbs_g": 61,
        "approximate": True,
    }]))
    coffee = {
        "name": "Кофе с молоком",
        "weight_g": 250,
        "portion_text": "кружка",
        "calories": 80,
        "protein_g": 4,
        "fat_g": 3,
        "carbs_g": 9,
        "approximate": False,
        "calculation_method": "reference",
        "fdc_id": 42,
    }
    items = [json.loads(result_json())["items"][0], coffee]

    result = ai.apply_clarification(
        "Замени гречку на гречневые хлопья 200 г",
        items,
        item_index=0,
    )

    assert result["operation"] == "replace"
    assert result["items"][0]["name"] == "Гречневые хлопья"
    assert result["items"][1] == coffee
    assert result["items"][1] is not coffee


def test_explicit_add_command_appends_instead_of_replacing():
    ai, _ = ai_with_response(result_json(items=[{
        "name": "Яблоко",
        "weight_g": 120,
        "portion_text": "одно яблоко",
        "calories": 62,
        "protein_g": 0.3,
        "fat_g": 0.2,
        "carbs_g": 17,
        "approximate": True,
    }]))
    original = [json.loads(result_json())["items"][0]]

    result = ai.apply_clarification("Добавь ещё яблоко 120 г", original)

    assert result["operation"] == "append"
    assert result["target_index"] is None
    assert [item["name"] for item in result["items"]] == ["Гречка", "Яблоко"]
    assert result["items"][1]["calculation_method"] == "ai"


def test_unclear_target_and_unclear_addition_never_change_items_or_call_ai():
    ai, responses = ai_with_response(result_json())
    multiple = [
        json.loads(result_json())["items"][0],
        {**json.loads(result_json())["items"][0], "name": "Курица"},
    ]

    with pytest.raises(NutritionAITargetAmbiguous) as caught:
        ai.apply_clarification("Это были хлопья", multiple)
    assert caught.value.candidates == (
        {"index": 0, "name": "Гречка"},
        {"index": 1, "name": "Курица"},
    )

    with pytest.raises(NutritionAIIntentAmbiguous, match="добавить"):
        ai.apply_clarification("А ещё яблоко", [multiple[0]])
    assert responses.calls == []


def test_mass_only_correction_keeps_name_and_uses_explicit_weight():
    ai, _ = ai_with_response(result_json(items=[{
        "name": "Другое блюдо",
        "weight_g": 150,
        "portion_text": "150 г",
        "calories": 390,
        "protein_g": 12,
        "fat_g": 11,
        "carbs_g": 59,
        "approximate": True,
    }]))
    original = [{
        **json.loads(result_json())["items"][0],
        "name": "Гречневые хлопья с семенами",
    }]

    corrected = ai.correct_draft_item("294 г", original)

    assert corrected["name"] == "Гречневые хлопья с семенами"
    assert corrected["weight_g"] == 294
    assert corrected["portion_text"] == "294 г"
    assert corrected["approximate"] is True


@pytest.mark.parametrize("instruction", ["масса -20г", "масса 0 г", "масса inf г"])
def test_invalid_explicit_weight_is_never_normalized_to_a_positive_value(instruction):
    ai, responses = ai_with_response(result_json())

    with pytest.raises(ValueError, match="масса"):
        ai.correct_draft_item(instruction, [json.loads(result_json())["items"][0]])
    assert responses.calls == []


def test_ai_replacement_and_prompt_whitelist_food_fields_and_remove_reference_provenance():
    ai, responses = ai_with_response(result_json())
    original = {
        **json.loads(result_json())["items"][0],
        "id": 77,
        "meal_id": 12,
        "created_at": "2030-01-01T00:00:00Z",
        "calculation_method": "reference",
        "reference_fdc_id": "12345",
        "reference_source": "USDA FoodData Central",
        "reference_url": "https://example.invalid/private-row",
    }

    corrected = ai.correct_draft_item("Уточни состав", [original])

    assert set(corrected) == {
        "name", "weight_g", "portion_text", "calories", "protein_g",
        "fat_g", "carbs_g", "approximate", "calculation_method",
    }
    assert corrected["calculation_method"] == "ai"
    prompt = responses.calls[0]["input"][1]["content"][0]["text"]
    assert "reference_fdc_id" not in prompt
    assert "USDA FoodData Central" not in prompt
    assert "created_at" not in prompt


def test_provider_failure_does_not_mutate_items_and_same_correction_can_be_retried():
    responses = SequencedResponses([
        TimeoutError("provider details"),
        result_json(items=[{
            "name": "Гречневые хлопья",
            "weight_g": 294,
            "portion_text": "294 г",
            "calories": 490,
            "protein_g": 15,
            "fat_g": 17,
            "carbs_g": 70,
            "approximate": True,
        }]),
    ])
    ai = NutritionAI(client=SimpleNamespace(responses=responses), model="vision-test")
    original = [json.loads(result_json())["items"][0]]
    snapshot = json.loads(json.dumps(original, ensure_ascii=False))

    with pytest.raises(NutritionAIUnavailable):
        ai.apply_clarification("Это гречневые хлопья 294 г", original)
    assert original == snapshot

    retried = ai.apply_clarification("Это гречневые хлопья 294 г", original)
    assert retried["items"][0]["name"] == "Гречневые хлопья"
    assert retried["items"][0]["weight_g"] == 294
    assert len(responses.calls) == 2


def test_replacement_rejects_multiple_ai_items_instead_of_keeping_old_and_new_foods():
    first = json.loads(result_json())["items"][0]
    second = {**first, "name": "Кедровые орехи"}
    ai, _ = ai_with_response(result_json(items=[first, second]))

    with pytest.raises(NutritionAIResponseError, match="ровно одно"):
        ai.correct_draft_item(
            "Это гречневые хлопья с семенами и кедровыми орехами 294г",
            [{**first, "name": "Овсянка с ягодами годжи"}],
        )
