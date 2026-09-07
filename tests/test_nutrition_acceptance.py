"""Критические сценарии приёмки дневника питания.

Тесты работают только с временной SQLite и не отправляют сообщения наружу.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.nutrition_store import NutritionStore  # noqa: E402
from src.nutrition_ai import NutritionAI, NutritionAIResponseError  # noqa: E402

try:
    import telegram  # noqa: F401
except ModuleNotFoundError:
    telegram_module = types.ModuleType("telegram")
    telegram_ext_module = types.ModuleType("telegram.ext")

    class _Button:
        def __init__(self, text, callback_data=None, web_app=None, url=None):
            self.text = text
            self.callback_data = callback_data
            self.web_app = web_app
            self.url = url

    class _Markup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class _WebAppInfo:
        def __init__(self, url):
            self.url = url

    telegram_module.InlineKeyboardButton = _Button
    telegram_module.InlineKeyboardMarkup = _Markup
    telegram_module.Update = object
    telegram_module.WebAppInfo = _WebAppInfo
    telegram_ext_module.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["telegram"] = telegram_module
    sys.modules["telegram.ext"] = telegram_ext_module

from src.nutrition_bot import NutritionBotController  # noqa: E402


ITEM = {
    "name": "Омлет",
    "portion_text": "одна тарелка",
    "calories": 320,
    "protein_g": 22,
    "fat_g": 21,
    "carbs_g": 8,
    "approximate": True,
}


class NutritionStoreAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "nutrition.sqlite3"
        self.store = NutritionStore(self.db_path)
        self.trainer_a = self.store.ensure_user(telegram_id=100, display_name="Тренер А")
        self.trainer_b = self.store.ensure_user(telegram_id=200, display_name="Тренер Б")
        self.client_a = self.store.ensure_user(telegram_id=1001, display_name="Клиент А")
        self.client_b = self.store.ensure_user(telegram_id=1002, display_name="Клиент Б")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def link(self, trainer_id: int, client_id: int) -> dict:
        invite = self.store.create_trainer_invite(trainer_telegram_id=trainer_id)
        return self.store.link_client_by_code(client_telegram_id=client_id, code=invite["code"])

    def draft(self, client_id: int, *, source: str = "manual") -> dict:
        return self.store.create_meal_draft(
            client_telegram_id=client_id,
            source=source,
            photo_file_id="telegram-file" if source == "photo" else None,
            eaten_at="2026-09-07T09:00:00+03:00",
            meal_type="завтрак",
            items=[ITEM],
        )

    def test_01_trainer_sees_only_linked_clients(self) -> None:
        self.link(100, 1001)
        self.link(200, 1002)
        self.assertEqual([self.client_a["id"]], [row["id"] for row in self.store.list_trainer_clients(100)])
        self.assertEqual([self.client_b["id"]], [row["id"] for row in self.store.list_trainer_clients(200)])
        with self.assertRaises(PermissionError):
            self.store.get_client_day(100, self.client_b["id"], "2026-09-07")

    def test_02_invite_rejects_unknown_expired_and_replayed_codes(self) -> None:
        with self.assertRaises(ValueError):
            self.store.link_client_by_code(client_telegram_id=1001, code="UNKNOWN")
        invite = self.store.create_trainer_invite(trainer_telegram_id=100, max_uses=1)
        self.store.link_client_by_code(client_telegram_id=1001, code=invite["code"])
        with self.assertRaises(ValueError):
            self.store.link_client_by_code(client_telegram_id=1002, code=invite["code"])
        expired = self.store.create_trainer_invite(trainer_telegram_id=100)
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "UPDATE trainer_invites SET expires_at = ? WHERE code = ?",
                ("2000-01-01T00:00:00+00:00", expired["code"]),
            )
        with self.assertRaises(ValueError):
            self.store.link_client_by_code(client_telegram_id=1002, code=expired["code"])

        concurrent_clients = []
        for offset in range(8):
            telegram_id = 3000 + offset
            self.store.ensure_user(telegram_id=telegram_id, display_name=f"Клиент {offset}")
            concurrent_clients.append(telegram_id)
        one_use = self.store.create_trainer_invite(trainer_telegram_id=100, max_uses=1)

        def consume(client_id: int) -> tuple[str, str]:
            try:
                self.store.link_client_by_code(client_telegram_id=client_id, code=one_use["code"])
                return ("ok", "")
            except Exception as exc:  # результат проверяется ниже по типу
                return (type(exc).__name__, str(exc))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(consume, concurrent_clients))
        self.assertEqual(1, sum(kind == "ok" for kind, _ in results), results)
        self.assertTrue(
            all(kind in {"ok", "ValueError"} for kind, _ in results),
            f"Конкурентное использование кода дало техническую ошибку: {results}",
        )

    def test_03_photo_requires_current_consent_and_supports_revoke(self) -> None:
        with self.assertRaises(PermissionError):
            self.draft(1001, source="photo")
        self.store.set_photo_consent(telegram_id=1001)
        self.assertTrue(self.store.has_photo_consent(telegram_id=1001))
        self.assertTrue(
            hasattr(self.store, "revoke_photo_consent"),
            "Нет публичной операции отзыва согласия на обработку фото",
        )
        self.store.revoke_photo_consent(telegram_id=1001)
        self.assertFalse(self.store.has_photo_consent(telegram_id=1001))
        with self.assertRaises(PermissionError):
            self.draft(1001, source="photo")

    def test_04_draft_is_excluded_until_confirmation(self) -> None:
        meal = self.draft(1001)
        self.assertEqual([], self.store.get_own_day(client_telegram_id=1001, local_date="2026-09-07")["meals"])
        confirmed = self.store.confirm_meal(
            client_telegram_id=1001,
            meal_id=meal["id"],
            idempotency_key="confirm-update-1",
        )
        self.assertEqual("confirmed", confirmed["status"])
        day = self.store.get_own_day(client_telegram_id=1001, local_date="2026-09-07")
        self.assertEqual(1, len(day["meals"]))
        self.assertEqual(1, day["meals"][0]["items"][0]["approximate"])

    def test_05_confirmation_water_and_cancel_replays_are_safe(self) -> None:
        meal = self.draft(1001)
        first = self.store.confirm_meal(client_telegram_id=1001, meal_id=meal["id"], idempotency_key="c-1")
        replay = self.store.confirm_meal(client_telegram_id=1001, meal_id=meal["id"], idempotency_key="c-1")
        self.assertEqual(first["id"], replay["id"])
        water_1 = self.store.add_water(
            client_telegram_id=1001,
            amount_ml=250,
            logged_at="2026-09-07T10:00:00+03:00",
            idempotency_key="water-update-1",
        )
        water_2 = self.store.add_water(
            client_telegram_id=1001,
            amount_ml=250,
            logged_at="2026-09-07T10:00:00+03:00",
            idempotency_key="water-update-1",
        )
        self.assertEqual(water_1["id"], water_2["id"])
        cancelled = self.draft(1001)
        self.store.cancel_meal(client_telegram_id=1001, meal_id=cancelled["id"])
        with self.assertRaises(ValueError):
            self.store.confirm_meal(client_telegram_id=1001, meal_id=cancelled["id"], idempotency_key="late")

    def test_06_client_cannot_edit_confirm_or_cancel_another_clients_meal(self) -> None:
        meal = self.draft(1001)
        calls = (
            lambda: self.store.replace_draft_items(client_telegram_id=1002, meal_id=meal["id"], items=[ITEM]),
            lambda: self.store.confirm_meal(client_telegram_id=1002, meal_id=meal["id"], idempotency_key="foreign"),
            lambda: self.store.cancel_meal(client_telegram_id=1002, meal_id=meal["id"]),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(PermissionError):
                call()

    def test_07_trainer_edits_and_comments_only_own_confirmed_meals(self) -> None:
        self.link(100, 1001)
        self.link(200, 1002)
        draft = self.draft(1001)
        with self.assertRaises(ValueError):
            self.store.update_meal_as_trainer(100, draft["id"], {"note": "рано"})
        meal = self.store.confirm_meal(client_telegram_id=1001, meal_id=draft["id"], idempotency_key="confirmed")
        with self.assertRaises(PermissionError):
            self.store.update_meal_as_trainer(200, meal["id"], {"note": "чужая запись"})
        with self.assertRaises(PermissionError):
            self.store.add_comment(200, meal["id"], "чужой комментарий")
        updated = self.store.update_meal_as_trainer(100, meal["id"], {"note": "проверено"})
        comment = self.store.add_comment(100, meal["id"], "Порция подтверждена")
        self.assertEqual("проверено", updated["note"])
        self.assertEqual("Порция подтверждена", comment["text"])

    def test_08_timezone_change_controls_new_local_dates(self) -> None:
        self.assertTrue(
            hasattr(self.store, "set_timezone"),
            "Нет отдельной операции смены часового пояса без риска сброса при /start",
        )
        self.store.set_timezone(telegram_id=1001, timezone_name="Asia/Tokyo")
        user = self.store.get_user_by_telegram_id(1001)
        self.assertEqual("Asia/Tokyo", user["timezone"])
        meal = self.store.create_meal_draft(
            client_telegram_id=1001,
            source="manual",
            eaten_at="2026-09-07T16:30:00+00:00",
            meal_type="ужин",
            items=[ITEM],
        )
        self.assertEqual("2026-09-08", meal["local_date"])

    def test_09_norm_history_uses_effective_date_without_rewriting_past(self) -> None:
        self.link(100, 1001)
        for invalid in (float("nan"), float("inf"), float("-inf"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.set_norms(100, self.client_a["id"], {"calories": invalid}, "2026-08-31")
        self.store.set_norms(100, self.client_a["id"], {"calories": 1800}, "2026-09-01")
        self.store.set_norms(100, self.client_a["id"], {"calories": 2000}, "2026-09-08")
        old = self.store.get_client_day(100, self.client_a["id"], "2026-09-07")
        new = self.store.get_client_day(100, self.client_a["id"], "2026-09-08")
        self.assertEqual(1800, old["norms"]["calories"])
        self.assertEqual(2000, new["norms"]["calories"])

    def test_10_unlink_immediately_revokes_trainer_access(self) -> None:
        self.link(100, 1001)
        self.link(200, 1001)
        self.assertEqual([], self.store.list_trainer_clients(100), "После перепривязки старый тренер сохранил доступ")
        self.assertEqual([self.client_a["id"]], [row["id"] for row in self.store.list_trainer_clients(200)])
        self.assertTrue(
            hasattr(self.store, "unlink_client"),
            "Нет публичной операции отвязки клиента от тренера",
        )
        self.store.unlink_client(trainer_telegram_id=200, client_id=self.client_a["id"])
        self.assertEqual([], self.store.list_trainer_clients(200))
        with self.assertRaises(PermissionError):
            self.store.get_client_day(200, self.client_a["id"], "2026-09-07")

    def test_11_malformed_ai_result_never_becomes_zero_filled_draft(self) -> None:
        malformed = (
            '{"items":[{"name":"Каша","portion_text":"тарелка",'
            '"protein_g":5,"fat_g":2,"carbs_g":30}],"questions":[]}',
            '{"items":[{"name":"Каша","portion_text":"тарелка",'
            '"calories":-1,"protein_g":5,"fat_g":2,"carbs_g":30}],"questions":[]}',
            '{"items":[{"name":"Каша","weight_g":null,"portion_text":"",'
            '"calories":200,"protein_g":5,"fat_g":2,"carbs_g":30}],"questions":[]}',
            '{"items":[{"name":"Каша","portion_text":"тарелка",'
            '"calories":200,"protein_g":5,"fat_g":2,"carbs_g":30}],"questions":"Уточните массу"}',
        )
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(NutritionAIResponseError):
                NutritionAI._parse_result(payload)

        claimed_exact = NutritionAI._parse_result(
            '{"items":[{"name":"Каша","weight_g":null,"portion_text":"тарелка",'
            '"calories":200,"protein_g":5,"fat_g":2,"carbs_g":30,'
            '"approximate":false}],"questions":[]}'
        )
        self.assertTrue(claimed_exact["items"][0]["approximate"])
class NutritionBotPrivateChatAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = NutritionStore(Path(self.tmp.name) / "nutrition.sqlite3")
        self.store.ensure_user(telegram_id=100, display_name="Тренер")
        self.store.ensure_user(telegram_id=1001, display_name="Клиент")
        self.controller = NutritionBotController(
            store=self.store,
            ai=NutritionAI(client=None, api_key=None),
            trainer_ids={100},
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def group_update(user_id: int, *, callback_data: str | None = None, photo: bool = False):
        message = SimpleNamespace(
            text=None,
            photo=[SimpleNamespace(file_id="group-photo")] if photo else [],
            reply_text=AsyncMock(),
        )
        query = None
        if callback_data is not None:
            query = SimpleNamespace(
                data=callback_data,
                id="group-query",
                message=message,
                edit_message_text=AsyncMock(),
            )
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, full_name="Участник", first_name="Участник"),
            effective_chat=SimpleNamespace(type="group", id=-12345),
            effective_message=message,
            message=message,
            callback_query=query,
        )

    async def test_12_group_chat_never_enters_or_discloses_nutrition_flow(self) -> None:
        start_context = SimpleNamespace(user_data={})
        await self.controller.start(self.group_update(1001), start_context)
        self.assertFalse(self.controller.is_active(start_context), "Групповой /start включил дневник")

        for data in (
            "nutrition:today",
            "nutrition:week",
            "nutrition:invite",
            "nutrition:trainer",
            "nutrition:meal_photo",
        ):
            with self.subTest(callback=data):
                context = SimpleNamespace(user_data={})
                update = self.group_update(100 if data in {"nutrition:invite", "nutrition:trainer"} else 1001, callback_data=data)
                await self.controller.handle_callback(update, context)
                self.assertFalse(self.controller.is_active(context), f"{data} включил дневник в группе")
                rendered = " ".join(
                    str(call.args[0])
                    for mock in (update.callback_query.edit_message_text, update.message.reply_text)
                    for call in mock.await_args_list
                    if call.args
                )
                self.assertNotIn("Код для привязки", rendered)
                self.assertNotIn("Итого по записям", rendered)

        photo_context = SimpleNamespace(user_data={"nutrition_active": True, "nutrition_state": "wait_photo"})
        photo_update = self.group_update(1001, photo=True)
        await self.controller.handle_photo(photo_update, photo_context)
        self.assertNotIn("nutrition_pending_photo", photo_context.user_data)


if __name__ == "__main__":
    unittest.main()
