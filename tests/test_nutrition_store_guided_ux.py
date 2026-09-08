from __future__ import annotations

import sqlite3

import pytest

from src.nutrition_store import NutritionStore


def prepared_store(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    trainer = store.ensure_user(telegram_id=101, display_name="Тренер")
    client = store.ensure_user(telegram_id=202, display_name="Клиент")
    store.ensure_user(telegram_id=303, display_name="Другой клиент")
    code = store.get_or_create_trainer_code(trainer_telegram_id=101)["code"]
    store.link_client_by_code(client_telegram_id=202, code=code)
    store.set_photo_consent(telegram_id=202)
    return store, trainer, client


def reference_item():
    return {
        "name": "Банан",
        "weight_g": 100,
        "portion_text": "1 небольшой банан",
        "calories": 89,
        "protein_g": 1.1,
        "fat_g": 0.3,
        "carbs_g": 22.8,
        "calculation_method": "reference",
        "reference_fdc_id": "173944",
        "reference_source": "USDA FoodData Central",
        "reference_version": "2026-04",
        "reference_url": "https://fdc.nal.usda.gov/fdc-app.html#/food-details/173944",
        "reference_description": "Bananas, raw",
        "reference_preparation": "raw",
        "reference_kcal_per_100g": 89,
        "reference_protein_per_100g": 1.1,
        "reference_fat_per_100g": 0.3,
        "reference_carbs_per_100g": 22.8,
    }


def confirmed_meal(store):
    draft = store.create_meal_draft(
        client_telegram_id=202,
        source="photo",
        photo_file_id="old-photo",
        eaten_at="2026-09-07T08:00:00+03:00",
        meal_type="breakfast",
        note="Старое примечание",
        hunger_level=8,
        mood="good",
        items=[
            {
                "name": "Каша",
                "weight_g": 250,
                "portion_text": "глубокая тарелка",
                "calories": 320,
                "protein_g": 10,
                "fat_g": 8,
                "carbs_g": 52,
                "calculation_method": "ai",
                "approximate": True,
            },
            reference_item(),
        ],
    )
    meal = store.confirm_meal(
        client_telegram_id=202,
        meal_id=draft["id"],
        idempotency_key="confirm-source",
    )
    store.add_comment(101, meal["id"], "Добавьте белок")
    return store.get_own_meal(client_telegram_id=202, meal_id=meal["id"])


def test_comments_and_recent_meals_remain_visible_to_client_after_unlink(tmp_path):
    store, _, client = prepared_store(tmp_path)
    meal = confirmed_meal(store)
    store.unlink_client(trainer_telegram_id=101, client_id=client["id"])

    comments = store.list_own_trainer_comments(client_telegram_id=202)
    assert comments == [{
        "id": meal["comments"][0]["id"],
        "meal_id": meal["id"],
        "text": "Добавьте белок",
        "created_at": meal["comments"][0]["created_at"],
        "trainer_id": 1,
        "trainer_display_name": "Тренер",
        "meal": {
            "id": meal["id"],
            "eaten_at": meal["eaten_at"],
            "local_date": "2026-09-07",
            "timezone": "Europe/Moscow",
            "meal_type": "breakfast",
        },
    }]
    assert store.list_own_recent_meals(client_telegram_id=202)[0]["id"] == meal["id"]
    with pytest.raises(PermissionError):
        store.get_client_meal(101, meal["id"])


def test_repeat_is_new_unconfirmed_idempotent_draft_with_source_snapshots(tmp_path):
    store, _, _ = prepared_store(tmp_path)
    original = confirmed_meal(store)

    repeated = store.repeat_own_meal(
        client_telegram_id=202,
        meal_id=original["id"],
        eaten_at="2026-09-08T09:30:00+03:00",
        idempotency_key="repeat-one",
    )
    replay = store.repeat_own_meal(
        client_telegram_id=202,
        meal_id=original["id"],
        eaten_at="2026-09-08T09:30:00+03:00",
        idempotency_key="repeat-one",
    )

    assert replay["id"] == repeated["id"]
    assert repeated["id"] != original["id"]
    assert repeated["status"] == "draft"
    assert repeated["source"] == "photo"
    assert repeated["photo_file_id"] is None
    assert repeated["local_date"] == "2026-09-08"
    assert repeated["note"] == ""
    assert repeated["hunger_level"] is None
    assert repeated["mood"] is None
    assert repeated["comments"] == []
    assert [item["calculation_method"] for item in repeated["items"]] == ["ai", "reference"]
    for key in (
        "reference_fdc_id", "reference_source", "reference_version", "reference_url",
        "reference_description", "reference_preparation", "reference_kcal_per_100g",
        "reference_protein_per_100g", "reference_fat_per_100g",
        "reference_carbs_per_100g",
    ):
        assert repeated["items"][1][key] == original["items"][1][key]

    assert store.get_own_day(
        client_telegram_id=202, local_date="2026-09-08"
    )["meals"] == []
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM meals WHERE status='confirmed'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM meals WHERE status='draft'").fetchone()[0] == 1

    with pytest.raises(RuntimeError, match="idempotency_conflict"):
        store.repeat_own_meal(
            client_telegram_id=202,
            meal_id=original["id"],
            eaten_at="2026-09-08T10:00:00+03:00",
            idempotency_key="repeat-one",
        )
    with pytest.raises(PermissionError):
        store.repeat_own_meal(
            client_telegram_id=303,
            meal_id=original["id"],
            eaten_at="2026-09-08T09:30:00+03:00",
            idempotency_key="foreign-repeat",
        )


def test_repeated_reference_portion_edit_keeps_original_snapshot(tmp_path):
    store, _, _ = prepared_store(tmp_path)
    original = confirmed_meal(store)
    repeated = store.repeat_own_meal(
        client_telegram_id=202,
        meal_id=original["id"],
        eaten_at="2026-09-08T09:30:00+03:00",
        idempotency_key="repeat-edit",
    )
    items = []
    for item in repeated["items"]:
        items.append({
            "id": item["id"],
            "name": item["name"],
            "weight_g": 150 if item["calculation_method"] == "reference" else item["weight_g"],
            "portion_text": item["portion_text"],
            "calories": item["calories"],
            "protein_g": item["protein_g"],
            "fat_g": item["fat_g"],
            "carbs_g": item["carbs_g"],
            "approximate": False,
        })
    changed = store.update_meal_as_client(
        client_telegram_id=202,
        meal_id=repeated["id"],
        updates={"items": items},
        expected_version=repeated["version"],
    )
    reference = changed["items"][1]
    assert reference["calculation_method"] == "reference"
    assert reference["reference_version"] == original["items"][1]["reference_version"]
    assert reference["weight_g"] == 150
    assert reference["calories"] == 133.5
    assert changed["items"][0]["calculation_method"] == "ai"
    assert changed["items"][0]["approximate"] == 1


def test_replace_draft_items_rejects_stale_ai_result_without_mutation(tmp_path):
    store, _, _ = prepared_store(tmp_path)
    draft = store.create_meal_draft(
        client_telegram_id=202,
        source="manual",
        eaten_at="2026-09-08T09:30:00+03:00",
        meal_type="breakfast",
        items=[{
            "name": "Исходная оценка",
            "portion_text": "1 порция",
            "calories": 200,
            "protein_g": 10,
            "fat_g": 5,
            "carbs_g": 30,
            "calculation_method": "ai",
            "approximate": True,
        }],
    )
    fresh = store.replace_draft_items(
        client_telegram_id=202,
        meal_id=draft["id"],
        expected_version=draft["version"],
        items=[{
            "id": draft["items"][0]["id"],
            "name": "Ручная правка",
            "portion_text": "1 большая порция",
            "calories": 240,
            "protein_g": 12,
            "fat_g": 6,
            "carbs_g": 36,
        }],
    )

    stale_ai_items = [{
        "id": fresh["items"][0]["id"],
        "name": "Запоздалый ответ ИИ",
        "portion_text": "1 порция",
        "calories": 210,
        "protein_g": 11,
        "fat_g": 5,
        "carbs_g": 31,
    }]
    with pytest.raises(RuntimeError, match="version_conflict"):
        store.replace_draft_items(
            client_telegram_id=202,
            meal_id=draft["id"],
            expected_version=draft["version"],
            items=stale_ai_items,
        )

    current = store.get_owned_draft(client_telegram_id=202, meal_id=draft["id"])
    assert current["version"] == fresh["version"]
    assert current["items"] == fresh["items"]
