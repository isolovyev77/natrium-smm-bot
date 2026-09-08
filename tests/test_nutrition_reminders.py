from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from src.nutrition_reminders import ReminderEngine, ReminderLoop
from src.nutrition_store import NutritionStore


def _user(store: NutritionStore, telegram_id: int, timezone_name: str = "Europe/Moscow") -> dict:
    return store.ensure_user(
        telegram_id=telegram_id,
        display_name=f"User {telegram_id}",
        timezone_name=timezone_name,
    )


def test_schema_three_migrates_to_four_without_changing_existing_user(tmp_path):
    path = tmp_path / "nutrition.sqlite3"
    store = NutritionStore(path)
    before = _user(store, 101)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE nutrition_client_reminder_preferences")
        db.execute("DROP TABLE nutrition_trainer_reminder_preferences")
        db.execute("DROP TABLE nutrition_notification_outbox")
        db.execute("DROP TABLE nutrition_mutation_dedup")
        db.execute("UPDATE schema_meta SET version=3")
    reopened = NutritionStore(path)
    assert reopened.get_user_by_telegram_id(101) == before
    assert reopened.get_client_reminder_preferences(telegram_id=101)["enabled"] is False
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_client_preferences_are_versioned_idempotent_and_cancel_old_queue(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    user = _user(store, 101)
    saved = store.update_client_reminder_preferences(
        telegram_id=101,
        updates={
            "enabled": True,
            "meal": {"enabled": True, "times": ["12:00", "08:00", "08:00"]},
            "water": {
                "enabled": True, "mode": "interval", "interval_minutes": 120,
                "start_local": "09:00", "end_local": "21:00",
            },
            "quiet_hours": {"start": "22:00", "end": "07:00"},
        },
        expected_version=0,
        idempotency_key="settings-1",
    )
    assert saved["version"] == 1
    assert saved["meal"]["times"] == ["08:00", "12:00"]
    repeated = store.update_client_reminder_preferences(
        telegram_id=101,
        updates={
            "enabled": True,
            "meal": {"enabled": True, "times": ["08:00", "12:00"]},
            "water": {
                "enabled": True, "mode": "interval", "interval_minutes": 120,
                "start_local": "09:00", "end_local": "21:00",
            },
            "quiet_hours": {"start": "22:00", "end": "07:00"},
        },
        expected_version=0,
        idempotency_key="settings-1",
    )
    assert repeated["version"] == 1
    with pytest.raises(RuntimeError, match="idempotency_conflict"):
        store.update_client_reminder_preferences(
            telegram_id=101,
            updates={"enabled": False},
            expected_version=1,
            idempotency_key="settings-1",
        )
    assert store.enqueue_notification(
        recipient_user_id=user["id"], subject_user_id=user["id"], kind="meal",
        local_date="2026-09-08", slot_key="08:00", preferences_scope="client",
        preferences_version=1, scheduled_for="2026-09-08T05:00:00+00:00",
    )
    store.update_client_reminder_preferences(
        telegram_id=101, updates={"enabled": False}, expected_version=1,
        idempotency_key="settings-2",
    )
    assert store.claim_due_notifications(
        now="2026-09-08T05:01:00+00:00"
    ) == []


def test_engine_sends_once_skips_quiet_hours_and_does_not_catch_up(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    _user(store, 101)
    store.update_client_reminder_preferences(
        telegram_id=101,
        updates={
            "enabled": True,
            "meal": {"enabled": True, "times": ["08:00"]},
            "water": {"enabled": True, "mode": "times", "times": ["06:30", "08:00"]},
            "quiet_hours": {"start": "22:00", "end": "07:00"},
        },
        expected_version=0,
        idempotency_key="settings",
    )
    sent: list[tuple[int, str]] = []
    engine = ReminderEngine(store, lambda user_id, text: sent.append((user_id, text)), allowed_trainer_ids=set())
    now = datetime(2026, 9, 8, 5, 5, tzinfo=timezone.utc)  # 08:05 Moscow
    first = engine.tick(now=now)
    second = engine.tick(now=now)
    assert first == {"scheduled": 2, "sent": 2, "cancelled": 0, "failed": 0}
    assert second == {"scheduled": 0, "sent": 0, "cancelled": 0, "failed": 0}
    assert len(sent) == 2
    assert engine.tick(now=datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc))["sent"] == 0


def test_async_engine_awaits_sender(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    _user(store, 101)
    store.update_client_reminder_preferences(
        telegram_id=101,
        updates={"enabled": True, "meal": {"enabled": True, "times": ["08:00"]}},
        expected_version=0,
        idempotency_key="settings",
    )
    sent = []

    async def sender(user_id, text):
        sent.append((user_id, text))

    engine = ReminderEngine(store, sender, allowed_trainer_ids=set())
    result = asyncio.run(engine.tick_async(
        now=datetime(2026, 9, 8, 5, 5, tzinfo=timezone.utc)
    ))
    assert result["sent"] == 1
    assert sent[0][0] == 101


def test_reminder_loop_starts_once_and_stops_without_telegram():
    class FakeEngine:
        def __init__(self):
            self.called = asyncio.Event()
            self.calls = 0

        async def tick_async(self):
            self.calls += 1
            self.called.set()

    async def scenario():
        engine = FakeEngine()
        loop = ReminderLoop(engine, interval_seconds=3600)
        loop.start()
        loop.start()
        await asyncio.wait_for(engine.called.wait(), timeout=1)
        assert loop.running is True
        assert engine.calls == 1
        await loop.stop()
        assert loop.running is False

    asyncio.run(scenario())


def test_source_pagination_reaches_client_after_first_five_hundred(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    now = "2026-09-08T00:00:00+00:00"
    with sqlite3.connect(store.db_path) as db:
        db.executemany(
            """
            INSERT INTO users(telegram_id, display_name, timezone, created_at, updated_at)
            VALUES (?, ?, 'Europe/Moscow', ?, ?)
            """,
            [(10_000 + index, f"Synthetic {index}", now, now) for index in range(501)],
        )
        rows = db.execute("SELECT id FROM users ORDER BY id").fetchall()
        db.executemany(
            """
                INSERT INTO nutrition_client_reminder_preferences(
                    user_id, enabled, meal_enabled, meal_times_json,
                    water_enabled, water_mode, water_times_json,
                    version, updated_at
                ) VALUES (?, 1, 1, '[\"08:00\"]', 0, 'times', '[]', 1, ?)
                """,
            [(row[0], now) for row in rows],
        )
    engine = ReminderEngine(
        store, lambda _user_id, _text: None,
        allowed_trainer_ids=set(), source_batch_size=500,
    )
    at_slot = datetime(2026, 9, 8, 5, 5, tzinfo=timezone.utc)
    assert engine.schedule_due(now=at_slot) == 500
    assert engine.schedule_due(now=at_slot) == 1
    assert engine.schedule_due(now=at_slot) == 0
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM nutrition_notification_outbox"
        ).fetchone()[0] == 501


def test_trainer_digest_requires_current_allowlist_and_relationship(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    trainer = _user(store, 201)
    client = _user(store, 202)
    code = store.get_or_create_trainer_code(trainer_telegram_id=201)["code"]
    store.link_client_by_code(client_telegram_id=202, code=code)
    saved = store.update_trainer_reminder_preferences(
        trainer_telegram_id=201,
        updates={
            "enabled": True,
            "daily_digest": {"enabled": True, "time": "20:00", "inactivity_days": 2},
        },
        expected_version=0,
        idempotency_key="digest-1",
    )
    assert saved["version"] == 1
    now = datetime(2026, 9, 8, 17, 5, tzinfo=timezone.utc)
    blocked: list[tuple[int, str]] = []
    engine = ReminderEngine(store, lambda uid, text: blocked.append((uid, text)), allowed_trainer_ids=set())
    assert engine.tick(now=now)["sent"] == 0
    assert blocked == []
    allowed: list[tuple[int, str]] = []
    engine = ReminderEngine(store, lambda uid, text: allowed.append((uid, text)), allowed_trainer_ids={201})
    assert engine.tick(now=now)["sent"] == 1
    assert allowed and "Без записей питания" in allowed[0][1]
    store.unlink_client(trainer_telegram_id=201, client_id=client["id"])
    assert store.list_trainer_clients(201) == []


def test_timezone_change_cancels_pending_slot(tmp_path):
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    user = _user(store, 101)
    store.update_client_reminder_preferences(
        telegram_id=101,
        updates={"enabled": True, "meal": {"enabled": True, "times": ["08:00"]}},
        expected_version=0,
        idempotency_key="settings",
    )
    assert store.enqueue_notification(
        recipient_user_id=user["id"], subject_user_id=user["id"], kind="meal",
        local_date="2026-09-08", slot_key="08:00", preferences_scope="client",
        preferences_version=1, scheduled_for="2026-09-08T05:00:00+00:00",
    )
    store.set_timezone(telegram_id=101, timezone_name="Asia/Yekaterinburg")
    assert store.claim_due_notifications(now="2026-09-08T05:01:00+00:00") == []


def test_draft_and_comment_create_are_durably_idempotent(tmp_path):
    path = tmp_path / "nutrition.sqlite3"
    store = NutritionStore(path)
    trainer = _user(store, 201)
    client = _user(store, 202)
    code = store.get_or_create_trainer_code(trainer_telegram_id=201)["code"]
    store.link_client_by_code(client_telegram_id=202, code=code)
    kwargs = {
        "client_telegram_id": 202,
        "source": "manual",
        "eaten_at": "2026-09-08T08:00:00+03:00",
        "meal_type": "breakfast",
        "items": [{
            "name": "Каша", "weight_g": 200, "calories": 250,
            "protein_g": 8, "fat_g": 5, "carbs_g": 45,
        }],
        "idempotency_key": "draft-one",
    }
    first = store.create_meal_draft(**kwargs)
    second = NutritionStore(path).create_meal_draft(**kwargs)
    assert first["id"] == second["id"]
    with pytest.raises(RuntimeError, match="idempotency_conflict"):
        store.create_meal_draft(**{**kwargs, "meal_type": "lunch"})
    comment = store.add_comment(201, first["id"], "Хорошо", idempotency_key="comment-one")
    repeated = NutritionStore(path).add_comment(
        201, first["id"], "Хорошо", idempotency_key="comment-one"
    )
    assert comment["id"] == repeated["id"]
    with pytest.raises(RuntimeError, match="idempotency_conflict"):
        store.add_comment(201, first["id"], "Другой текст", idempotency_key="comment-one")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trainer_comments").fetchone()[0] == 1
