import json
from types import SimpleNamespace

import pytest

from src.nutrition_ai import (
    NutritionAI,
    NutritionAIResponseError,
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
