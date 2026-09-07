import csv
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from src.nutrition_reference import (
    FoodNotFoundError,
    FoodReference,
    IncompleteNutrientsError,
    InvalidAmountError,
    ReferenceDataNotFoundError,
)


@pytest.fixture(scope="module")
def reference():
    return FoodReference()


def test_known_usda_sr_legacy_values_are_preserved(reference):
    buckwheat = reference.get("170686")
    assert buckwheat.description == "Buckwheat groats, roasted, cooked"
    assert buckwheat.per_100g.kcal == Decimal("92")
    assert buckwheat.per_100g.protein_g == Decimal("3.38")
    assert buckwheat.per_100g.fat_g == Decimal("0.62")
    assert buckwheat.per_100g.carbs_g == Decimal("19.94")
    assert buckwheat.source == "USDA FoodData Central SR Legacy"
    assert buckwheat.version == "2018-04"


def test_foundation_energy_prefers_specific_atwater_value(reference):
    buckwheat = reference.get("2512378")
    assert buckwheat.description == "Buckwheat, whole grain"
    assert buckwheat.per_100g.kcal == Decimal("331.6005206")
    assert buckwheat.version == "2026-04-30"


def test_calculation_for_150_and_200_grams_is_decimal_and_stable(reference):
    buckwheat = reference.calculate("170686", 150)
    assert buckwheat.grams == Decimal("150")
    assert buckwheat.totals.kcal == Decimal("138.00")
    assert buckwheat.totals.protein_g == Decimal("5.07")
    assert buckwheat.totals.fat_g == Decimal("0.93")
    assert buckwheat.totals.carbs_g == Decimal("29.91")

    rice = reference.calculate(168878, Decimal("200"))
    assert rice.totals.kcal == Decimal("260.00")
    assert rice.totals.protein_g == Decimal("5.38")
    assert rice.totals.fat_g == Decimal("0.56")
    assert rice.totals.carbs_g == Decimal("56.34")
    assert rice.calculation_method == "reference"


def test_russian_aliases_return_stable_ids_and_keep_variants(reference):
    cooked = reference.search("гречка варёная")
    assert cooked[0].fdc_id == "170686"
    assert "cooked" in cooked[0].preparation

    chicken = reference.search("куриная грудка")
    assert [item.fdc_id for item in chicken[:2]] == ["171477", "171077"]
    assert chicken[0].description != chicken[1].description

    oils = reference.search("масло")
    assert [item.fdc_id for item in oils[:4]] == ["173410", "173430", "171413", "171017"]


def test_english_search_preserves_raw_and_cooked_qualifiers(reference):
    raw = reference.search("rice raw", limit=10)
    cooked = reference.search("rice cooked", limit=10)
    assert raw and cooked
    assert all("raw" in item.description.casefold() for item in raw)
    assert all("cooked" in item.description.casefold() for item in cooked)
    assert {item.fdc_id for item in raw}.isdisjoint({item.fdc_id for item in cooked})


def test_numeric_qualifier_is_not_dropped_or_falsely_translated(reference):
    assert reference.search("творог 9%") == []
    assert reference.search("cottage cheese 9%") == []
    four_percent = reference.search("cottage cheese 4%", limit=10)
    assert all("4" in item.description for item in four_percent)


@pytest.mark.parametrize(
    "query, expected_id",
    [
        ("яблоко", "171688"),
        ("банан", "173944"),
        ("овсяная каша", "173905"),
        ("картофель сырой", "170026"),
        ("молоко 2%", "746778"),
        ("йогурт натуральный", "2259793"),
        ("белый хлеб", "174924"),
        ("говяжий фарш сырой", "2514743"),
        ("лосось сырой", "175167"),
        ("треска готовая", "175178"),
        ("помидор", "170457"),
        ("огурец", "2346406"),
        ("морковь варёная", "170394"),
        ("брокколи сырая", "747447"),
        ("апельсин", "169097"),
        ("авокадо", "171705"),
    ],
)
def test_curated_russian_basics_map_to_verified_records(reference, query, expected_id):
    result = reference.search(query)
    assert result and result[0].fdc_id == expected_id
    assert result[0].description


def test_fat_percentage_and_preparation_are_not_silently_changed(reference):
    assert reference.search("молоко 2%")[0].fdc_id == "746778"
    assert reference.search("молоко 3,25%")[0].fdc_id == "746782"
    assert reference.search("молоко 9%") == []
    assert reference.search("овсяные хлопья сухие")[0].fdc_id == "173904"
    assert reference.search("овсяная каша на воде")[0].fdc_id == "173905"


def test_empty_search_and_nonpositive_limit_are_empty(reference):
    assert reference.search("") == []
    assert reference.search("   ") == []
    assert reference.search("rice", limit=0) == []
    assert reference.search("rice", limit=-1) == []


@pytest.mark.parametrize("fdc_id", ["999999999", "bad", "", 0, -1, True])
def test_unknown_or_invalid_id_is_explicit(reference, fdc_id):
    with pytest.raises(FoodNotFoundError):
        reference.get(fdc_id)


@pytest.mark.parametrize(
    "grams", [0, -1, "NaN", float("nan"), float("inf"), float("-inf"), "bad", True, 100001, "1e1000"]
)
def test_invalid_weight_is_rejected(reference, grams):
    with pytest.raises(InvalidAmountError):
        reference.calculate("170686", grams)


def test_incomplete_record_is_never_calculated(tmp_path):
    headers = [
        "fdc_id", "source", "version", "url", "description", "display_name",
        "preparation", "aliases", "kcal", "protein_g", "fat_g", "carbs_g",
        "energy_nutrient_id",
    ]
    with (tmp_path / "catalog.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerow({
            "fdc_id": "42", "source": "test", "version": "1", "url": "https://example.test/42",
            "description": "Incomplete", "display_name": "Incomplete", "kcal": "10",
            "protein_g": "1", "fat_g": "2", "carbs_g": "",
        })
    (tmp_path / "aliases.json").write_text(
        json.dumps({"schema_version": 1, "aliases": []}), encoding="utf-8"
    )
    (tmp_path / "provenance.json").write_text("{}", encoding="utf-8")
    incomplete = FoodReference(tmp_path)
    assert incomplete.search("Incomplete") == []
    with pytest.raises(IncompleteNutrientsError):
        incomplete.get("42")
    with pytest.raises(IncompleteNutrientsError):
        incomplete.calculate("42", 100)


def test_missing_resource_dir_is_explicit(tmp_path):
    with pytest.raises(ReferenceDataNotFoundError):
        FoodReference(tmp_path / "missing")


def test_result_contains_provenance_and_is_immutable(reference):
    result = reference.calculate("168878", 100)
    assert result.source == "USDA FoodData Central SR Legacy"
    assert result.version == "2018-04"
    assert result.url.endswith("/168878/nutrients")
    assert result.per_100g == result.totals
    with pytest.raises(FrozenInstanceError):
        result.grams = Decimal("200")
