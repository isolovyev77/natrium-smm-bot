from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from src.nutrition_store import NutritionStore


def prepared_store(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    trainer = store.ensure_user(telegram_id=101, display_name="Тренер")
    client = store.ensure_user(telegram_id=202, display_name="Клиент")
    other = store.ensure_user(telegram_id=303, display_name="Другой тренер")
    code = store.get_or_create_trainer_code(trainer_telegram_id=101)["code"]
    store.link_client_by_code(client_telegram_id=202, code=code)
    return store, trainer, client, other


def test_v2_database_migrates_atomically_and_preserves_values(tmp_path):
    db_path = tmp_path / "v2.sqlite3"
    old = NutritionStore(db_path)
    old.ensure_user(telegram_id=1, display_name="До миграции")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE schema_meta SET version=2")
        for table in (
            "nutrition_profiles", "weight_logs", "nutrition_web_login_tokens",
            "nutrition_web_sessions", "nutrition_plan_previews",
        ):
            db.execute(f"DROP TABLE {table}")
        # SQLite cannot drop newly added meal columns, so emulate a genuine v2 table.
        db.execute("ALTER TABLE meals RENAME TO meals_v3")
        db.execute(
            """
            CREATE TABLE meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,client_user_id INTEGER NOT NULL,
                source TEXT NOT NULL,photo_file_id TEXT,eaten_at TEXT NOT NULL,
                local_date TEXT NOT NULL,timezone TEXT NOT NULL,meal_type TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,created_at TEXT NOT NULL,
                confirmed_at TEXT,confirmation_key TEXT,updated_at TEXT NOT NULL,
                UNIQUE(client_user_id,confirmation_key)
            )
            """
        )
        db.execute("DROP TABLE meals_v3")
        for table in ("water_logs",):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            assert {"status", "version", "cancelled_at"}.issubset(columns)
    migrated = NutritionStore(db_path)
    assert migrated.get_user_by_telegram_id(1)["display_name"] == "До миграции"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_profile_version_and_browser_login_are_one_time(tmp_path):
    store, _, _, _ = prepared_store(tmp_path)
    profile = store.update_profile(
        telegram_id=202,
        updates={"height_cm": 171, "goal": "Режим", "initial_weight_kg": 70},
        expected_version=0,
    )
    assert profile["version"] == 1
    with pytest.raises(RuntimeError, match="stale_version"):
        store.update_profile(
            telegram_id=202, updates={"goal": "Другое"}, expected_version=0
        )
    login = store.create_browser_login_token(telegram_id=202)
    session = "s" * 43
    csrf = "c" * 64
    exchanged = store.exchange_browser_login_token(
        login_token=login["token"], session_token=session, csrf_token=csrf
    )
    assert exchanged["user"]["telegram_id"] == 202
    assert store.authenticate_browser_session(session_token=session)["telegram_id"] == 202
    assert store.validate_browser_csrf(session_token=session, csrf_token=csrf)
    with pytest.raises(PermissionError):
        store.exchange_browser_login_token(
            login_token=login["token"], session_token="x" * 43, csrf_token="y" * 64
        )


def test_manual_profile_name_survives_telegram_refresh_and_username_can_clear(tmp_path):
    store, _, _, _ = prepared_store(tmp_path)
    store.ensure_user(
        telegram_id=202, display_name="Имя Telegram", telegram_username="old_handle"
    )
    profile = store.update_profile(
        telegram_id=202, updates={"display_name": "Имя в дневнике"}, expected_version=0
    )
    assert profile["display_name"] == "Имя в дневнике"
    refreshed = store.ensure_user(
        telegram_id=202, display_name="Снова Telegram", telegram_username=None
    )
    assert refreshed["display_name"] == "Имя в дневнике"
    assert refreshed["telegram_username"] is None


def test_telegram_name_refreshes_until_user_sets_manual_name(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    store.ensure_user(telegram_id=202, display_name="Первое имя")
    refreshed = store.ensure_user(telegram_id=202, display_name="Новое имя")
    assert refreshed["display_name"] == "Новое имя"


def test_water_and_weight_can_be_corrected_then_soft_cancelled(tmp_path):
    store, _, _, _ = prepared_store(tmp_path)
    at = datetime(2026, 9, 8, 9, tzinfo=timezone.utc)
    water = store.add_water(
        client_telegram_id=202, amount_ml=250, logged_at=at, idempotency_key="water-1"
    )
    water = store.update_water(
        client_telegram_id=202, water_id=water["id"], amount_ml=300,
        logged_at=at, expected_version=1,
    )
    assert water["amount_ml"] == 300 and water["version"] == 2
    water = store.cancel_water(
        client_telegram_id=202, water_id=water["id"], expected_version=2
    )
    assert water["status"] == "cancelled"
    weight = store.add_weight(
        client_telegram_id=202, weight_kg=70, measured_at=at,
        idempotency_key="weight-1", note="утро",
    )
    weight = store.update_weight(
        client_telegram_id=202, weight_id=weight["id"], weight_kg=69.8,
        measured_at=at, note="после сна", expected_version=1,
    )
    assert weight["weight_kg"] == 69.8
    assert store.cancel_weight(
        client_telegram_id=202, weight_id=weight["id"], expected_version=2
    )["status"] == "cancelled"


def test_period_summary_uses_metric_specific_logged_days(tmp_path):
    store, _, client, _ = prepared_store(tmp_path)
    store.set_norms(
        trainer_telegram_id=101, client_id=client["id"],
        effective_from="2026-08-20",
        norms={
            "calories": 1800, "protein_g": 110, "fat_g": 60,
            "carbs_g": 210, "water_ml": 2000,
        },
    )
    store.set_norms(
        trainer_telegram_id=101, client_id=client["id"],
        effective_from="2026-09-02",
        norms={
            "calories": 2000, "protein_g": 120, "fat_g": 70,
            "carbs_g": 230, "water_ml": 2200,
        },
    )
    meal = store.create_meal_draft(
        client_telegram_id=202, source="manual",
        eaten_at="2026-09-01T09:00:00+03:00", meal_type="завтрак",
        items=[{
            "name": "Блюдо", "weight_g": 100, "calories": 600,
            "protein_g": 20, "fat_g": 10, "carbs_g": 50,
            "calculation_method": "manual", "approximate": False,
        }],
    )
    store.confirm_meal(client_telegram_id=202, meal_id=meal["id"], idempotency_key="meal-1")
    store.add_water(
        client_telegram_id=202, amount_ml=500,
        logged_at="2026-09-02T09:00:00+03:00", idempotency_key="water-1",
    )
    store.add_weight(
        client_telegram_id=202, weight_kg=70,
        measured_at="2026-09-03T09:00:00+03:00", idempotency_key="weight-1",
    )
    result = store.get_own_summary(
        client_telegram_id=202, date_from="2026-09-01", date_to="2026-09-04"
    )
    assert result["averages"]["calories"] == 600
    assert result["averages"]["water_ml"] == 500
    assert result["averages"]["weight_kg"] == 70
    assert result["coverage"]["calories"] == {"logged_days": 1, "period_days": 4}
    assert result["series"][1]["calories"] is None
    assert result["series"][1]["water_ml"] == 500
    assert result["series"][0]["norms"]["calories"] == 1800
    assert result["series"][1]["norms"]["calories"] == 2000


def test_confirmed_meal_client_edit_is_versioned_and_audited(tmp_path):
    store, _, _, _ = prepared_store(tmp_path)
    meal = store.create_meal_draft(
        client_telegram_id=202, source="manual",
        eaten_at="2026-09-08T10:00:00+03:00", meal_type="обед",
        items=[{"name":"Блюдо","weight_g":100,"calories":100,"protein_g":1,"fat_g":2,"carbs_g":3}],
    )
    meal = store.confirm_meal(
        client_telegram_id=202, meal_id=meal["id"], idempotency_key="confirm"
    )
    changed = store.update_meal_as_client(
        client_telegram_id=202, meal_id=meal["id"],
        updates={"note":"уточнение","hunger_level":7,"mood":"good"},
        expected_version=meal["version"],
    )
    assert changed["status"] == "confirmed" and changed["version"] == meal["version"] + 1
    history = store.get_meal_history(101, meal["id"])
    assert history[-1]["action"] == "client_update"


def test_plan_preview_is_bound_to_actor_client_and_consumed_once(tmp_path):
    store, _, client, _ = prepared_store(tmp_path)
    preview = store.create_plan_preview(
        trainer_telegram_id=101, client_id=client["id"],
        rows=[{
            "effective_from":"2026-09-10","calories":2000,"protein_g":120,
            "fat_g":70,"carbs_g":220,"water_ml":2000,
        }],
    )
    with pytest.raises(PermissionError):
        store.commit_plan_preview(
            trainer_telegram_id=303, client_id=client["id"],
            upload_token=preview["upload_token"],
        )
    rows = store.commit_plan_preview(
        trainer_telegram_id=101, client_id=client["id"],
        upload_token=preview["upload_token"],
    )
    assert rows[0]["calories"] == 2000
    with pytest.raises(PermissionError):
        store.commit_plan_preview(
            trainer_telegram_id=101, client_id=client["id"],
            upload_token=preview["upload_token"],
        )
