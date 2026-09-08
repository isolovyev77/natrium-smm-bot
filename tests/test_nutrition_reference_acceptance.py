from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.nutrition_store import NutritionStore


FIXTURE = Path(__file__).parent / "fixtures" / "nutrition_v1_full.sql"


def _restore_v1(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        db.executescript(FIXTURE.read_text())


def _snapshot_v1(db_path: Path) -> dict[str, dict]:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: {
                "columns": [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')],
                "rows": [dict(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')],
            }
            for table in tables
        }


def _reference_item(*, kcal: float, protein: float, fat: float, carbs: float) -> dict:
    return {
        "name": "Синтетический продукт справочника",
        "weight_g": 200,
        "portion_text": "",
        "calculation_method": "reference",
        "reference_fdc_id": "420042",
        "reference_source": "Синтетический справочник",
        "reference_version": "fixture-v1",
        "reference_url": "https://example.invalid/420042",
        "reference_description": "Тестовая запись",
        "reference_preparation": "как указано",
        "reference_kcal_per_100g": kcal,
        "reference_protein_per_100g": protein,
        "reference_fat_per_100g": fat,
        "reference_carbs_per_100g": carbs,
    }


def test_complete_real_v1_fixture_migrates_without_data_loss(tmp_path: Path) -> None:
    db_path = tmp_path / "nutrition.sqlite3"
    _restore_v1(db_path)
    before = _snapshot_v1(db_path)

    assert {name: len(data["rows"]) for name, data in before.items()} == {
        "audit_log": 6,
        "meal_items": 1,
        "meals": 1,
        "nutrition_norms": 1,
        "photo_consents": 1,
        "schema_meta": 1,
        "trainer_clients": 1,
        "trainer_comments": 1,
        "trainer_invites": 1,
        "users": 2,
        "water_logs": 1,
    }
    old_invite = before["trainer_invites"]["rows"][0]
    assert old_invite["uses"] == old_invite["max_uses"] == 1
    assert before["meals"]["rows"][0]["status"] == "confirmed"

    store = NutritionStore(db_path)
    after = _snapshot_v1(db_path)

    assert set(before).issubset(after)
    assert {
        "nutrition_profiles", "weight_logs", "nutrition_web_login_tokens",
        "nutrition_web_sessions", "nutrition_plan_previews",
    }.issubset(after)
    for table, old in before.items():
        assert len(after[table]["rows"]) == len(old["rows"]), table
        if table == "schema_meta":
            continue
        old_columns = old["columns"]
        projected = [
            {column: row[column] for column in old_columns}
            for row in after[table]["rows"]
        ]
        assert projected == old["rows"], table

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        migrated_item = db.execute("SELECT * FROM meal_items").fetchone()
        assert migrated_item["calculation_method"] == "ai"
        assert all(
            migrated_item[name] is None
            for name in (
                "reference_fdc_id",
                "reference_source",
                "reference_version",
                "reference_url",
                "reference_description",
                "reference_preparation",
                "reference_kcal_per_100g",
                "reference_protein_per_100g",
                "reference_fat_per_100g",
                "reference_carbs_per_100g",
            )
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    store.ensure_user(telegram_id=73001, display_name="Новый клиент")
    with pytest.raises(ValueError):
        store.link_client_by_code(client_telegram_id=73001, code=old_invite["code"])


def test_reference_snapshot_survives_restart_and_later_catalogue_values(tmp_path: Path) -> None:
    db_path = tmp_path / "nutrition.sqlite3"
    store = NutritionStore(db_path)
    store.ensure_user(telegram_id=81001, display_name="Клиент справочника")
    first = store.create_meal_draft(
        client_telegram_id=81001,
        source="manual",
        eaten_at=datetime(2026, 9, 8, 8, tzinfo=timezone.utc),
        meal_type="завтрак",
        items=[_reference_item(kcal=100, protein=10, fat=4, carbs=8)],
    )
    confirmed = store.confirm_meal(
        client_telegram_id=81001,
        meal_id=first["id"],
        idempotency_key="reference-fixture-confirm",
    )
    original = confirmed["items"][0]

    reopened = NutritionStore(db_path)
    later = reopened.create_meal_draft(
        client_telegram_id=81001,
        source="manual",
        eaten_at=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        meal_type="обед",
        items=[_reference_item(kcal=140, protein=12, fat=6, carbs=9)],
    )

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        old_row = dict(db.execute(
            "SELECT * FROM meal_items WHERE meal_id = ?", (first["id"],)
        ).fetchone())
        new_row = dict(db.execute(
            "SELECT * FROM meal_items WHERE meal_id = ?", (later["id"],)
        ).fetchone())
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    for field in (
        "calories",
        "protein_g",
        "fat_g",
        "carbs_g",
        "reference_kcal_per_100g",
        "reference_protein_per_100g",
        "reference_fat_per_100g",
        "reference_carbs_per_100g",
        "reference_version",
    ):
        assert old_row[field] == original[field]
    assert old_row["reference_kcal_per_100g"] == 100
    assert old_row["calories"] == 200
    assert new_row["reference_kcal_per_100g"] == 140
    assert new_row["calories"] == 280
