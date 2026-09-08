from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.nutrition_store import NutritionStore


def reference_item(*, fdc_id: str = "12345", grams: float = 250) -> dict:
    return {
        "name": "Творог, 4% жирности",
        "weight_g": grams,
        "portion_text": "",
        "calculation_method": "reference",
        "reference_fdc_id": fdc_id,
        "reference_source": "USDA FoodData Central",
        "reference_version": "2026-04",
        "reference_url": f"https://fdc.nal.usda.gov/fdc-app.html#/food-details/{fdc_id}/nutrients",
        "reference_description": "Cottage cheese, creamed, large or small curd",
        "reference_preparation": "as sold",
        "reference_kcal_per_100g": 98,
        "reference_protein_per_100g": 11.1,
        "reference_fat_per_100g": 4.3,
        "reference_carbs_per_100g": 3.4,
    }


def make_store(tmp_path: Path) -> NutritionStore:
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    store.ensure_user(telegram_id=1001, display_name="Клиент")
    return store


def create_reference_draft(store: NutritionStore, telegram_id: int = 1001) -> dict:
    return store.create_meal_draft(
        client_telegram_id=telegram_id,
        source="manual",
        eaten_at=datetime.now(timezone.utc),
        meal_type="перекус",
        items=[reference_item()],
    )


def test_v1_migration_preserves_rows_and_marks_old_invites_one_time(tmp_path: Path) -> None:
    db_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE schema_meta(version INTEGER NOT NULL);
            INSERT INTO schema_meta VALUES (1);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL UNIQUE,
                display_name TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO users VALUES (
                1,100,'Тренер','Europe/Moscow','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z'
            );
            CREATE TABLE meal_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, meal_id INTEGER NOT NULL,
                name TEXT NOT NULL, weight_g REAL, portion_text TEXT NOT NULL DEFAULT '',
                calories REAL NOT NULL, protein_g REAL NOT NULL, fat_g REAL NOT NULL,
                carbs_g REAL NOT NULL, approximate INTEGER NOT NULL DEFAULT 1,
                manually_edited INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            INSERT INTO meal_items(
                meal_id,name,weight_g,portion_text,calories,protein_g,fat_g,carbs_g,
                approximate,manually_edited,created_at
            ) VALUES (77,'Старая запись',100,'',120,10,5,7,1,0,'2026-09-07T00:00:00Z');
            CREATE TABLE trainer_invites (
                code TEXT PRIMARY KEY, trainer_user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL, max_uses INTEGER NOT NULL DEFAULT 1,
                uses INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            INSERT INTO trainer_invites VALUES (
                'GMVSIMNB',1,'2026-09-07T00:00:00+00:00',1,1,'2026-09-01T00:00:00+00:00'
            );
            """
        )

    NutritionStore(db_path)

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        item = dict(db.execute("SELECT * FROM meal_items WHERE meal_id=77").fetchone())
        assert item["name"] == "Старая запись"
        assert item["calories"] == 120
        assert item["calculation_method"] == "legacy"
        assert item["reference_fdc_id"] is None
        invite = dict(db.execute("SELECT * FROM trainer_invites WHERE code='GMVSIMNB'").fetchone())
        assert invite["invite_kind"] == "one_time"
        assert invite["uses"] == 1
        assert invite["revoked_at"] is None


def test_reference_snapshot_recalculates_and_manual_override_clears_provenance(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    meal = create_reference_draft(store)
    item = meal["items"][0]
    assert item["calculation_method"] == "reference"
    assert item["calories"] == 245
    assert item["protein_g"] == 27.75
    assert item["approximate"] == 0

    changed = store.update_reference_item_weight(
        client_telegram_id=1001, meal_id=meal["id"], item_id=item["id"], grams="125"
    )
    changed_item = changed["items"][0]
    assert changed_item["calories"] == 122.5
    assert changed_item["protein_g"] == 13.88
    assert changed_item["reference_kcal_per_100g"] == 98

    manual = store.replace_draft_items(
        client_telegram_id=1001,
        meal_id=meal["id"],
        items=[{
            "name": "Творог 9% по упаковке", "weight_g": 250, "portion_text": "",
            "calories": 397.5, "protein_g": 41.75, "fat_g": 22.5, "carbs_g": 5,
            "approximate": False,
        }],
    )["items"][0]
    assert manual["calculation_method"] == "manual"
    assert manual["reference_fdc_id"] is None
    assert manual["reference_kcal_per_100g"] is None
    assert manual["manually_edited"] == 1


def test_v1_migration_rolls_back_as_one_unit_and_can_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "interrupted.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.executescript(
            """
            CREATE TABLE schema_meta(version INTEGER NOT NULL);
            INSERT INTO schema_meta VALUES (1);
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL UNIQUE,
                display_name TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE trainer_invites (
                code TEXT PRIMARY KEY, trainer_user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL, max_uses INTEGER NOT NULL DEFAULT 1,
                uses INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            INSERT INTO trainer_invites VALUES (
                'BROKENOLD',999,'2027-01-01T00:00:00+00:00',1,0,'2026-09-01T00:00:00+00:00'
            );
            CREATE TABLE meal_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, meal_id INTEGER NOT NULL,
                name TEXT NOT NULL, weight_g REAL, portion_text TEXT NOT NULL DEFAULT '',
                calories REAL NOT NULL, protein_g REAL NOT NULL, fat_g REAL NOT NULL,
                carbs_g REAL NOT NULL, approximate INTEGER NOT NULL DEFAULT 1,
                manually_edited INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        NutritionStore(db_path)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT version FROM schema_meta").fetchone()[0] == 1
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "trainer_invites" in tables
        assert "trainer_invites_v1" not in tables
        columns = {row[1] for row in db.execute("PRAGMA table_info(meal_items)")}
        assert "calculation_method" not in columns
        db.execute(
            "INSERT INTO users VALUES (999,999,'Тренер','Europe/Moscow',?,?)",
            ("2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z",),
        )

    store = NutritionStore(db_path)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        assert db.execute(
            "SELECT invite_kind FROM trainer_invites WHERE code='BROKENOLD'"
        ).fetchone()[0] == "one_time"


def test_reference_item_access_and_draft_state_are_enforced(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.ensure_user(telegram_id=2002, display_name="Другой клиент")
    own = create_reference_draft(store)
    foreign = create_reference_draft(store, telegram_id=2002)
    with pytest.raises(PermissionError):
        store.update_reference_item_weight(
            client_telegram_id=1001,
            meal_id=foreign["id"],
            item_id=foreign["items"][0]["id"],
            grams=300,
        )
    store.confirm_meal(client_telegram_id=1001, meal_id=own["id"], idempotency_key="confirm")
    with pytest.raises(ValueError):
        store.update_reference_item_weight(
            client_telegram_id=1001,
            meal_id=own["id"],
            item_id=own["items"][0]["id"],
            grams=300,
        )


def test_multiple_reference_products_confirm_and_cancel(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    meal = store.create_meal_draft(
        client_telegram_id=1001,
        source="manual",
        eaten_at=datetime.now(timezone.utc),
        meal_type="завтрак",
        items=[reference_item(fdc_id="111", grams=100), reference_item(fdc_id="222", grams=200)],
    )
    assert len(meal["items"]) == 2
    confirmed = store.confirm_meal(
        client_telegram_id=1001, meal_id=meal["id"], idempotency_key="multi-confirm"
    )
    assert confirmed["status"] == "confirmed"
    other = create_reference_draft(store)
    assert store.cancel_meal(client_telegram_id=1001, meal_id=other["id"])["status"] == "cancelled"


def test_reusable_code_concurrent_clients_replacement_and_old_one_time(tmp_path: Path) -> None:
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    store.ensure_user(telegram_id=100, display_name="Тренер")
    client_ids = list(range(200, 208))
    for telegram_id in client_ids:
        store.ensure_user(telegram_id=telegram_id, display_name=f"Клиент {telegram_id}")
    code = store.get_or_create_trainer_code(trainer_telegram_id=100)["code"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda cid: store.link_client_by_code(client_telegram_id=cid, code=code), client_ids))
    assert len(results) == 8
    assert len(store.list_trainer_clients(100)) == 8
    assert store.link_client_by_code(client_telegram_id=200, code=code)["client_id"]

    replacement = store.replace_trainer_code(trainer_telegram_id=100)["code"]
    assert replacement != code
    store.ensure_user(telegram_id=209, display_name="Новый клиент")
    with pytest.raises(ValueError):
        store.link_client_by_code(client_telegram_id=209, code=code)
    assert len(store.list_trainer_clients(100)) == 8

    one_time = store.create_trainer_invite(trainer_telegram_id=100, max_uses=1)["code"]
    store.ensure_user(telegram_id=300, display_name="Разовый клиент")
    store.ensure_user(telegram_id=301, display_name="Второй разовый клиент")
    store.link_client_by_code(client_telegram_id=300, code=one_time)
    with pytest.raises(ValueError):
        store.link_client_by_code(client_telegram_id=301, code=one_time)


def test_bulk_norms_are_atomic_and_do_not_delete_other_dates(tmp_path: Path) -> None:
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    trainer = store.ensure_user(telegram_id=100, display_name="Тренер")
    client = store.ensure_user(telegram_id=200, display_name="Клиент")
    invite = store.create_trainer_invite(trainer_telegram_id=100)
    store.link_client_by_code(client_telegram_id=200, code=invite["code"])
    store.set_norms(100, client["id"], {"calories": 2000}, "2026-09-01")

    with pytest.raises(ValueError):
        store.set_norms_bulk(100, client["id"], [
            {"effective_from": "2026-09-10", "calories": 2100},
            {"effective_from": "2026-09-11", "calories": -1},
        ])
    assert store.get_client_day(100, client["id"], "2026-09-10")["norms"]["effective_from"] == "2026-09-01"

    applied = store.set_norms_bulk(100, client["id"], [
        {"effective_from": "2026-09-10", "calories": 2100, "protein_g": 140},
        {"effective_from": "2026-09-20", "calories": 2200, "protein_g": 150},
    ])
    assert [row["effective_from"] for row in applied] == ["2026-09-10", "2026-09-20"]
    assert store.get_client_day(100, client["id"], "2026-09-05")["norms"]["effective_from"] == "2026-09-01"
