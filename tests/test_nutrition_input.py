import math

import pytest

from src.nutrition_input import parse_manual_items


NAME = "Гречневые хлопья с семенами и кедровыми орехами"


@pytest.mark.parametrize(
    "text",
    [
        (
            "🍽 Черновик #2\n"
            "завтрак, 08.09.2026 10:21\n"
            f"• {NAME}, 294 г: 400 ккал, Б 12, Ж 18, У 50"
        ),
        f"• {NAME}, 294 г: 400 ккал, Б 12, Ж 18, У 50",
        (
            f"{NAME},\n"
            "294 г:\n"
            "400 ккал,\n"
            "Б 12,\n"
            "Ж 18,\n"
            "У 50"
        ),
        (
            f"{NAME} и ягодами годжи\n"
            "294\n"
            "400\n"
            "12\n"
            "18\n"
            "50"
        ),
        f"{NAME};294;400;12;18;50",
    ],
)
def test_required_exact_manual_formats(text):
    item = parse_manual_items(text)[0]

    assert item["name"].startswith(NAME)
    assert item["weight_g"] == 294
    assert item["portion_text"] == ""
    assert item["calories"] == 400
    assert item["protein_g"] == 12
    assert item["fat_g"] == 18
    assert item["carbs_g"] == 50
    assert item["approximate"] is False
    assert "calculation_method" not in item
    assert "reference_source" not in item


def test_copied_own_bot_footer_is_ignored_and_keeps_approximate_hint():
    text = (
        "🍽️ <b>Черновик #2</b>\n"
        "завтрак, 08.09.2026 10:21\n"
        f"• {NAME}, 294 г: 400 ккал, Б 12, Ж 18, У 50\n"
        "⚠️ Оценка приблизительная. Проверьте блюдо и размер порции.\n"
        "Запись попадет в статистику только после подтверждения."
    )

    item = parse_manual_items(text)[0]

    assert item["name"] == NAME
    assert item["approximate"] is True


def test_multiple_dishes_require_and_respect_explicit_boundaries():
    bullets = (
        "• Гречка, 180 г: 190 ккал, Б 6, Ж 2, У 38\n"
        "• Курица, 120 г: 198 ккал, Б 37, Ж 4, У 0"
    )
    positional = (
        "Гречка\n180\n190\n6\n2\n38\n"
        "Курица\n120\n198\n37\n4\n0"
    )

    assert [item["name"] for item in parse_manual_items(bullets)] == ["Гречка", "Курица"]
    assert [item["name"] for item in parse_manual_items(positional)] == ["Гречка", "Курица"]

    with pytest.raises(ValueError, match="где заканчивается блюдо"):
        parse_manual_items(
            "Гречка, 180 г: 190 ккал, Б 6, Ж 2, У 38 "
            "Курица, 120 г: 198 ккал, Б 37, Ж 4, У 0"
        )


def test_decimal_commas_and_units_are_supported():
    item = parse_manual_items("Йогурт;125,5 г;98,4 ккал;5,2 г;3,1 г;12,8 г")[0]

    assert item["weight_g"] == 125.5
    assert item["calories"] == 98.4
    assert item["protein_g"] == 5.2
    assert item["fat_g"] == 3.1
    assert item["carbs_g"] == 12.8


def test_unknown_portion_is_preserved_without_guessing_weight():
    item = parse_manual_items("Борщ;одна глубокая тарелка;250;8;12;30")[0]

    assert item["weight_g"] is None
    assert item["portion_text"] == "одна глубокая тарелка"
    assert item["approximate"] is True


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Гречка;180;190;6;2", "углеводы"),
        ("Гречка;180;;6;2;38", "калорийность"),
        ("Гречка, 180 г: 190 ккал, Б 6, Ж 2", "углеводы"),
        ("Гречка\n180\n190\n6\n2", "углеводы"),
    ],
)
def test_missing_fields_name_the_specific_field_and_show_an_example(text, message):
    with pytest.raises(ValueError, match=message) as caught:
        parse_manual_items(text)
    assert "Пример:" in str(caught.value)


@pytest.mark.parametrize("bad", ["nan", "inf", "-1"])
def test_macros_must_be_finite_and_nonnegative(bad):
    with pytest.raises(ValueError, match="калорийность"):
        parse_manual_items(f"Гречка;180;{bad};6;2;38")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Гречка;08.09.2026;190;6;2;38", "даты или ID"),
        ("Гречка;#2;190;6;2;38", "даты или ID"),
        ("Гречка;180;08.09.2026;6;2;38", "калорийность"),
        ("Гречка;180;#2;6;2;38", "калорийность"),
    ],
)
def test_date_and_id_are_not_accepted_as_weight_or_macros(text, message):
    with pytest.raises(ValueError, match=message):
        parse_manual_items(text)


def test_ambiguous_extra_line_is_rejected_instead_of_joined_to_the_name():
    with pytest.raises(ValueError, match="где заканчивается блюдо"):
        parse_manual_items("Гречка\nс грибами\n180\n190\n6\n2\n38")


def test_decimal_commas_in_copied_draft_do_not_split_name_or_weight():
    item = parse_manual_items(
        "Хлопья, 294,5 г: 400,5 ккал, Б 12,5, Ж 18,5, У 50,5"
    )[0]

    assert item["name"] == "Хлопья"
    assert item["weight_g"] == 294.5
    assert item["calories"] == 400.5
    assert item["protein_g"] == 12.5
    assert item["fat_g"] == 18.5
    assert item["carbs_g"] == 50.5


def test_unknown_tail_after_labeled_macros_is_not_silently_discarded():
    with pytest.raises(ValueError, match="дополнительное блюдо отдельной строкой"):
        parse_manual_items(
            "Хлопья, 294 г: 400 ккал, Б 12, Ж 18, У 50, добавь яблоко"
        )


def test_six_labeled_lines_are_parsed_positionally_before_joined_form():
    item = parse_manual_items(
        "Хлопья\n294 г\n400 ккал\nБ 12\nЖ 18\nУ 50"
    )[0]

    assert item == {
        "name": "Хлопья",
        "weight_g": 294,
        "portion_text": "",
        "calories": 400,
        "protein_g": 12,
        "fat_g": 18,
        "carbs_g": 50,
        "approximate": False,
    }


@pytest.mark.parametrize(
    "portion",
    ["08.09.2026 10:21", "#2", "08.09.2026"],
)
def test_date_and_id_are_not_accepted_as_weight_or_portion(portion):
    with pytest.raises(ValueError, match="даты или ID"):
        parse_manual_items(f"Гречка;{portion};190;6;2;38")


def test_numeric_values_are_not_shifted_when_a_field_is_missing():
    with pytest.raises(ValueError, match="углеводы"):
        parse_manual_items("Гречка\n180\n190\n6\n2")


def test_zero_mass_without_portion_is_rejected_but_zero_macros_are_valid():
    with pytest.raises(ValueError, match="Масса должна быть больше нуля"):
        parse_manual_items("Кофе;0;0;0;0;0")

    item = parse_manual_items("Кофе;чашка;0;0;0;0")[0]
    assert item["weight_g"] is None
    assert item["portion_text"] == "чашка"
    assert all(math.isfinite(item[key]) for key in ("calories", "protein_g", "fat_g", "carbs_g"))
