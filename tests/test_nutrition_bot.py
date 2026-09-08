import asyncio
import html
import importlib.util
import re
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

try:
    from telegram import InlineKeyboardButton  # noqa: F401
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

try:
    from telegram.error import TelegramError
except ModuleNotFoundError:
    telegram_error_module = types.ModuleType("telegram.error")

    class TelegramError(Exception):
        pass

    telegram_error_module.TelegramError = TelegramError
    sys.modules["telegram.error"] = telegram_error_module

from src.nutrition_ai import NutritionAIUnavailable
from src.nutrition_bot import NutritionBotController, PHOTO_CONSENT_VERSION
import src.nutrition_bot as nutrition_bot_module
from src.nutrition_store import NutritionStore


CLIENT_ID = 1001
TRAINER_ID = 2001


class FakeMessage:
    def __init__(self, text=None, photo=None, document=None, message_id=10):
        self.text = text
        self.photo = photo or []
        self.document = document
        self.message_id = message_id
        self.outbound = []
        self.documents = []

    async def reply_text(self, text, **kwargs):
        self.outbound.append((text, kwargs))

    async def reply_document(self, document, **kwargs):
        self.documents.append((document, kwargs))


class FakeQuery:
    def __init__(self, data, message=None, query_id="callback-1"):
        self.data = data
        self.message = message or FakeMessage()
        self.id = query_id
        self.edits = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeTelegramFile:
    def __init__(self, payload=b"image"):
        self.payload = payload

    async def download_as_bytearray(self):
        return bytearray(self.payload)


class FakeBot:
    def __init__(self, file_payload=b"image", error=None):
        self.file_payload = file_payload
        self.error = error
        self.file_ids = []

    async def get_file(self, file_id):
        self.file_ids.append(file_id)
        if self.error:
            raise self.error
        return FakeTelegramFile(self.file_payload)


class FakeAI:
    available = True
    timeout_seconds = 0.1

    def __init__(self, *, result=None, error=None):
        self.result = result or {
            "items": [
                {
                    "name": "Гречка",
                    "weight_g": 180,
                    "portion_text": "",
                    "calories": 190,
                    "protein_g": 6,
                    "fat_g": 2,
                    "carbs_g": 38,
                    "approximate": True,
                }
            ],
            "questions": [],
        }
        self.error = error
        self.text_calls = []
        self.photo_calls = []
        self.clarification_calls = []
        self.correction_calls = []

    def analyze_text(self, text):
        self.text_calls.append(text)
        if self.error:
            raise self.error
        return self.result

    def analyze_photo(self, image, mime_type):
        self.photo_calls.append((image, mime_type))
        if self.error:
            raise self.error
        return self.result

    def apply_clarification(self, answer, current_items, *, item_index=None):
        self.clarification_calls.append((answer, current_items, item_index))
        if self.error:
            raise self.error
        return self.result

    def correct_draft_item(self, instruction, current_items, item_index=None):
        self.correction_calls.append((instruction, current_items, item_index))
        if self.error:
            raise self.error
        return dict(self.result["items"][0])


class DisabledAI(FakeAI):
    available = False


def make_update(
    *,
    user_id=CLIENT_ID,
    chat_type="private",
    text=None,
    photo_file_id=None,
    document=None,
    callback_data=None,
    callback_id="callback-1",
    message_id=10,
):
    photo = [] if photo_file_id is None else [SimpleNamespace(file_id=photo_file_id)]
    message = FakeMessage(text=text, photo=photo, document=document, message_id=message_id)
    query = FakeQuery(callback_data, message, callback_id) if callback_data is not None else None
    user = SimpleNamespace(id=user_id, full_name=f"User {user_id}", first_name="User")
    chat = SimpleNamespace(type=chat_type)
    return SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        effective_message=message,
        message=message,
        callback_query=query,
    )


def make_context(*, state=None, bot=None):
    user_data = dict(state or {})
    return SimpleNamespace(user_data=user_data, bot=bot or FakeBot(), args=[])


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def store(tmp_path):
    return NutritionStore(tmp_path / "nutrition.sqlite3")


def controller(store, ai=None, trainer_ids=None, help_service=None, dashboard_origin=None):
    return NutritionBotController(
        store=store,
        ai=ai or DisabledAI(),
        trainer_ids=set(trainer_ids or []),
        help_service=help_service,
        dashboard_origin=dashboard_origin,
    )


def nutrition_callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def test_private_start_opens_diary_without_consuming_unrelated_smm_updates(store):
    ctl = controller(store)
    context = make_context()
    update = make_update()

    run(ctl.start(update, context))

    assert context.user_data["nutrition_active"] is True
    text, kwargs = update.message.outbound[-1]
    assert "Дневник питания" in text
    assert "Первый шаг" in text
    assert "подтвержденные записи" in text
    assert "нормы задает тренер" in text
    assert "nutrition:add_meal" in nutrition_callbacks(kwargs["reply_markup"])
    assert "nutrition:link" in nutrition_callbacks(kwargs["reply_markup"])
    assert run(ctl.handle_callback(make_update(callback_data="smm:themes"), context)) is False
    assert run(ctl.handle_text(make_update(text="обычный SMM запрос"), make_context())) is False


def test_returning_menu_separates_daily_actions_from_settings_and_keeps_old_callbacks(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})

    menu = make_update(callback_data="nutrition:menu")
    run(ctl.handle_callback(menu, context))
    text, kwargs = menu.callback_query.edits[-1]
    callbacks = nutrition_callbacks(kwargs["reply_markup"])
    assert "Первый шаг" not in text
    assert "nutrition:add_meal" in callbacks
    assert "nutrition:comments" in callbacks
    assert "nutrition:repeat" in callbacks
    assert "nutrition:settings" in callbacks
    assert "nutrition:profile" not in callbacks
    assert "nutrition:timezone" not in callbacks

    settings = make_update(callback_data="nutrition:settings")
    run(ctl.handle_callback(settings, context))
    settings_callbacks = nutrition_callbacks(settings.callback_query.edits[-1][1]["reply_markup"])
    assert "nutrition:profile" in settings_callbacks
    assert "nutrition:timezone" in settings_callbacks
    assert "nutrition:link" in settings_callbacks
    assert "nutrition:menu" in settings_callbacks

    old_profile = make_update(callback_data="nutrition:profile")
    run(ctl.handle_callback(old_profile, context))
    assert "Профиль" in old_profile.callback_query.message.outbound[-1][0]


def test_link_code_explains_visibility_norms_and_next_step(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    ctl._ensure_user(make_update(user_id=TRAINER_ID))
    invite = store.create_trainer_invite(trainer_telegram_id=TRAINER_ID)
    context = make_context(state={"nutrition_active": True})
    ctl._ensure_user(make_update())

    run(ctl.handle_callback(make_update(callback_data="nutrition:link"), context))
    linked = make_update(text=invite["code"])
    run(ctl.handle_text(linked, context))

    text, kwargs = linked.message.outbound[-1]
    assert "подтвержденные записи" in text
    assert "нормы задает тренер" in text
    assert "Следующий шаг" in text
    assert "nutrition:add_meal" in nutrition_callbacks(kwargs["reply_markup"])
    assert context.user_data == {"nutrition_active": True}


def test_group_start_is_redirected_without_creating_profile_or_active_state(store):
    ctl = controller(store)
    context = make_context()
    update = make_update(chat_type="group")

    run(ctl.start(update, context))

    assert context.user_data == {}
    assert store.get_user_by_telegram_id(CLIENT_ID) is None
    assert "только в личном чате" in update.message.outbound[-1][0]


def test_photo_requires_consent_and_revoke_removes_it(store):
    ctl = controller(store, FakeAI())
    context = make_context(state={"nutrition_active": True, "nutrition_state": "wait_photo"})
    ctl._ensure_user(make_update())
    photo = make_update(photo_file_id="file-1")

    assert run(ctl.handle_photo(photo, context)) is True
    assert context.user_data["nutrition_pending_photo"] == "file-1"
    consent_text = photo.message.outbound[-1][0]
    assert "передано искусственному интеллекту" in consent_text
    assert "Передавать фото ИИ?" in consent_text
    assert "OpenAI" not in consent_text
    assert not store.has_photo_consent(telegram_id=CLIENT_ID, version=PHOTO_CONSENT_VERSION)

    store.set_photo_consent(telegram_id=CLIENT_ID, version=PHOTO_CONSENT_VERSION)
    revoke = make_update(callback_data="nutrition:consent_revoke")
    assert run(ctl.handle_callback(revoke, context)) is True
    assert not store.has_photo_consent(telegram_id=CLIENT_ID, version=PHOTO_CONSENT_VERSION)
    assert "Согласие отозвано" in revoke.callback_query.edits[-1][0]


def test_consent_no_never_sends_followup_free_text_to_ai(store):
    ai = FakeAI()
    ctl = controller(store, ai)
    ctl._ensure_user(make_update())
    context = make_context(
        state={
            "nutrition_active": True,
            "nutrition_state": "wait_photo",
            "nutrition_pending_photo": "file-1",
        }
    )
    decline = make_update(callback_data="nutrition:consent_no")
    run(ctl.handle_callback(decline, context))

    manual = make_update(text="гречка и курица")
    run(ctl.handle_text(manual, context))

    assert ai.text_calls == []
    assert "Шаг 2 из 6" in manual.message.outbound[-1][0]
    assert "Масса" in manual.message.outbound[-1][0]


def test_confirmed_photo_is_counted_only_after_human_confirmation_and_is_idempotent(store):
    ai = FakeAI()
    ctl = controller(store, ai)
    context = make_context(
        state={"nutrition_active": True, "nutrition_state": "wait_photo"},
        bot=FakeBot(b"photo-bytes"),
    )
    ctl._ensure_user(make_update())
    store.set_photo_consent(telegram_id=CLIENT_ID, version=PHOTO_CONSENT_VERSION)
    photo = make_update(photo_file_id="file-1")

    run(ctl.handle_photo(photo, context))
    draft_text = photo.message.outbound[-1][0]
    meal_id = int(re.search(r"Черновик #(\d+)", draft_text).group(1))
    assert "Оценка приблизительная" in draft_text
    local_date = ctl._today(CLIENT_ID)
    assert store.get_own_day(client_telegram_id=CLIENT_ID, local_date=local_date)["meals"] == []

    for callback_id in ("confirm-a", "confirm-b"):
        update = make_update(
            callback_data=f"nutrition:confirm:{meal_id}", callback_id=callback_id
        )
        assert run(ctl.handle_callback(update, context)) is True
    day = store.get_own_day(client_telegram_id=CLIENT_ID, local_date=local_date)
    assert [meal["id"] for meal in day["meals"]] == [meal_id]
    assert ai.photo_calls == [(b"photo-bytes", "image/jpeg")]


def test_photo_clarification_replaces_wrong_hypothesis_instead_of_appending(store):
    wrong = {
        "name": "Овсянка с ягодами годжи", "weight_g": 150, "portion_text": "",
        "calories": 220, "protein_g": 7, "fat_g": 5, "carbs_g": 36,
        "approximate": True,
    }
    corrected = {
        "name": "Гречневые хлопья с семенами и кедровыми орехами",
        "weight_g": 294, "portion_text": "", "calories": 400,
        "protein_g": 12, "fat_g": 18, "carbs_g": 50, "approximate": True,
    }

    class ClarifyingAI(FakeAI):
        def apply_clarification(self, answer, current_items, *, item_index=None):
            self.clarification_calls.append((answer, current_items, item_index))
            return {
                "items": [corrected], "questions": [],
                "operation": "replace", "target_index": 0,
            }

    ai = ClarifyingAI(result={
        "items": [wrong],
        "questions": ["Какой именно ингредиент используется для овсянки?"],
    })
    ctl = controller(store, ai)
    ctl._ensure_user(make_update())
    store.set_photo_consent(telegram_id=CLIENT_ID, version=PHOTO_CONSENT_VERSION)
    context = make_context(
        state={"nutrition_active": True, "nutrition_state": "wait_photo"},
        bot=FakeBot(b"photo"),
    )

    run(ctl.handle_photo(make_update(photo_file_id="meal-photo"), context))
    assert context.user_data["nutrition_pending_items"] == [wrong]
    answer = "Это гречневые хлопья с семенами и кедровыми орехами 294г"
    clarified = make_update(text=answer)
    run(ctl.handle_text(clarified, context))

    meal_id = context.user_data["nutrition_draft_meal_id"]
    meal = store.get_owned_draft(client_telegram_id=CLIENT_ID, meal_id=meal_id)
    assert meal["status"] == "draft"
    assert len(meal["items"]) == 1
    assert meal["items"][0]["name"] == corrected["name"]
    assert meal["items"][0]["weight_g"] == 294
    assert all("Овсянка" not in item["name"] for item in meal["items"])
    assert ai.clarification_calls == [(answer, [wrong], None)]
    assert f"nutrition:confirm:{meal_id}" in nutrition_callbacks(
        clarified.message.outbound[-1][1]["reply_markup"]
    )


def test_plain_draft_correction_selects_one_item_and_requires_new_confirmation(store):
    correction = {
        "name": "Гречневые хлопья с семенами и кедровыми орехами",
        "weight_g": 294, "portion_text": "", "calories": 400,
        "protein_g": 12, "fat_g": 18, "carbs_g": 50, "approximate": True,
    }
    ai = FakeAI(result={"items": [correction], "questions": []})
    ctl = controller(store, ai)
    ctl._ensure_user(make_update())
    draft = store.create_meal_draft(
        client_telegram_id=CLIENT_ID,
        source="manual",
        eaten_at="2026-09-08T10:21:00+03:00",
        meal_type="завтрак",
        items=[
            {
                "name": "Яблоко", "weight_g": 100, "portion_text": "",
                "calories": 52, "protein_g": 0.3, "fat_g": 0.2, "carbs_g": 14,
                "approximate": False,
            },
            {
                "name": "Овсянка с ягодами годжи", "weight_g": 150,
                "portion_text": "", "calories": 220, "protein_g": 7,
                "fat_g": 5, "carbs_g": 36, "approximate": True,
            },
        ],
    )
    context = make_context(state={
        "nutrition_active": True,
        "nutrition_state": "draft",
        "nutrition_draft_meal_id": draft["id"],
    })
    instruction = "Это гречневые хлопья с семенами и кедровыми орехами 294г"
    asked = make_update(text=instruction)
    run(ctl.handle_text(asked, context))

    assert context.user_data["nutrition_state"] == "draft_correction_target"
    assert ai.correction_calls == []
    second = next(
        value
        for value in nutrition_callbacks(asked.message.outbound[-1][1]["reply_markup"])
        if value.endswith(":1")
    )
    selected = make_update(callback_data=second)
    run(ctl.handle_callback(selected, context))

    updated = store.get_owned_draft(client_telegram_id=CLIENT_ID, meal_id=draft["id"])
    assert [item["name"] for item in updated["items"]] == [
        "Яблоко", correction["name"],
    ]
    assert updated["items"][1]["weight_g"] == 294
    assert updated["status"] == "draft"
    assert ai.correction_calls[0][0] == instruction
    assert ai.correction_calls[0][2] == 1
    callbacks = nutrition_callbacks(selected.message.outbound[-1][1]["reply_markup"])
    assert f"nutrition:confirm:{draft['id']}" in callbacks


def test_ai_correction_does_not_overwrite_a_draft_changed_while_ai_runs(store):
    ai = FakeAI()
    ctl = controller(store, ai)
    ctl._ensure_user(make_update())
    draft = store.create_meal_draft(
        client_telegram_id=CLIENT_ID,
        source="manual",
        eaten_at="2026-09-08T10:21:00+03:00",
        meal_type="завтрак",
        items=[{
            "name": "Овсянка", "weight_g": 150, "portion_text": "",
            "calories": 500, "protein_g": 10, "fat_g": 20, "carbs_g": 30,
            "approximate": True,
        }],
    )

    def change_in_web_before_ai_returns(instruction, current_items, item_index=None):
        fresh = store.get_owned_draft(
            client_telegram_id=CLIENT_ID, meal_id=draft["id"]
        )
        web_items = [dict(item) for item in fresh["items"]]
        web_items[0]["name"] = "Исправлено в веб-кабинете"
        store.replace_draft_items(
            client_telegram_id=CLIENT_ID,
            meal_id=draft["id"],
            items=web_items,
            expected_version=fresh["version"],
        )
        return dict(ai.result["items"][0])

    ai.correct_draft_item = change_in_web_before_ai_returns
    context = make_context(state={
        "nutrition_active": True,
        "nutrition_state": "draft",
        "nutrition_draft_meal_id": draft["id"],
    })
    instruction = "Это гречневые хлопья 294 г"
    changed = make_update(text=instruction)
    run(ctl.handle_text(changed, context))

    fresh = store.get_owned_draft(client_telegram_id=CLIENT_ID, meal_id=draft["id"])
    assert fresh["items"][0]["name"] == "Исправлено в веб-кабинете"
    assert context.user_data["nutrition_state"] == "draft"
    assert context.user_data["nutrition_stale_correction_instruction"] == instruction
    text = changed.message.outbound[-1][0]
    assert "изменился" in text
    assert "не применено" in text
    assert instruction in text


def test_structured_manual_draft_can_be_cancelled_and_never_enters_totals(store):
    ctl = controller(store)
    context = make_context(state={"nutrition_active": True, "nutrition_state": "wait_manual"})
    ctl._ensure_user(make_update())
    message = make_update(text="Гречка; 180; 190; 6; 2; 38")

    run(ctl.handle_text(message, context))
    meal_id = int(re.search(r"Черновик #(\d+)", message.message.outbound[-1][0]).group(1))
    cancel = make_update(callback_data=f"nutrition:cancel:{meal_id}")
    run(ctl.handle_callback(cancel, context))

    day = store.get_own_day(client_telegram_id=CLIENT_ID, local_date=ctl._today(CLIENT_ID))
    assert day["meals"] == []
    assert day["totals"]["calories"] == 0


def test_guided_weight_edit_rescales_portion_and_preserves_other_items(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    draft = store.create_meal_draft(
        client_telegram_id=CLIENT_ID,
        source="manual",
        eaten_at="2026-09-08T10:21:00+03:00",
        meal_type="завтрак",
        items=[
            {
                "name": "Хлопья", "weight_g": 150, "portion_text": "",
                "calories": 500, "protein_g": 10, "fat_g": 20, "carbs_g": 30,
                "approximate": True,
            },
            {
                "name": "Ягоды", "weight_g": 50, "portion_text": "",
                "calories": 40, "protein_g": 1, "fat_g": 0, "carbs_g": 9,
                "approximate": False,
            },
        ],
    )
    context = make_context(state={
        "nutrition_active": True,
        "nutrition_state": "draft",
        "nutrition_draft_meal_id": draft["id"],
    })

    choose_item = make_update(callback_data=f"nutrition:edit:{draft['id']}")
    run(ctl.handle_callback(choose_item, context))
    first_item = next(
        value
        for value in nutrition_callbacks(choose_item.callback_query.edits[-1][1]["reply_markup"])
        if value.endswith(":0")
    )
    fields = make_update(callback_data=first_item)
    run(ctl.handle_callback(fields, context))
    weight_field = next(
        value
        for value in nutrition_callbacks(fields.message.outbound[-1][1]["reply_markup"])
        if value.endswith(":weight_g")
    )
    run(ctl.handle_callback(make_update(callback_data=weight_field), context))
    changed = make_update(text="294")
    run(ctl.handle_text(changed, context))

    updated = store.get_owned_draft(client_telegram_id=CLIENT_ID, meal_id=draft["id"])
    first, second = updated["items"]
    assert first["weight_g"] == 294
    assert first["calories"] == 980
    assert first["protein_g"] == 19.6
    assert first["fat_g"] == 39.2
    assert first["carbs_g"] == 58.8
    assert first["calculation_method"] == "manual"
    assert bool(first["approximate"]) is True
    assert second["name"] == "Ягоды"
    assert second["weight_g"] == 50
    assert second["calories"] == 40
    assert updated["status"] == "draft"
    assert f"nutrition:confirm:{draft['id']}" in nutrition_callbacks(
        changed.message.outbound[-1][1]["reply_markup"]
    )


@pytest.mark.parametrize(
    "payload",
    [
        (
            "🍽 Черновик #2\n"
            "завтрак, 08.09.2026 10:21\n"
            "• Гречневые хлопья с семенами и кедровыми орехами, 294 г: "
            "400 ккал, Б 12, Ж 18, У 50"
        ),
        (
            "• Гречневые хлопья с семенами и кедровыми орехами, 294 г: "
            "400 ккал, Б 12, Ж 18, У 50"
        ),
        (
            "Гречневые хлопья с семенами и кедровыми орехами,\n"
            "294 г:\n400 ккал,\nБ 12,\nЖ 18,\nУ 50"
        ),
        (
            "Гречневые хлопья с семенами и кедровыми орехами и ягодами годжи\n"
            "294\n400\n12\n18\n50"
        ),
        "Гречневые хлопья с семенами и кедровыми орехами;294;400;12;18;50",
    ],
)
def test_manual_parser_accepts_copied_draft_and_simple_offline_formats(payload):
    item = NutritionBotController.parse_structured_items(payload)[0]
    assert item["name"].startswith("Гречневые хлопья")
    assert item["weight_g"] == 294
    assert item["calories"] == 400
    assert item["protein_g"] == 12
    assert item["fat_g"] == 18
    assert item["carbs_g"] == 50


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ("Хлопья, 294 г: 400 ккал, Б 12, Ж 18", "углеводы"),
        ("Хлопья;294;nan;12;18;50", "калорийность"),
        ("Хлопья;294;400;-1;18;50", "белки"),
    ],
)
def test_manual_parser_rejects_missing_nonfinite_and_negative_values(payload, field):
    with pytest.raises(ValueError, match=field):
        NutritionBotController.parse_structured_items(payload)


def test_ai_failure_keeps_manual_route_and_does_not_create_draft(store):
    ai = FakeAI(error=NutritionAIUnavailable("provider details"))
    ctl = controller(store, ai)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True, "nutrition_state": "wait_manual"})
    message = make_update(text="обычный текст про обед")

    assert run(ctl.handle_text(message, context)) is True
    assert context.user_data["nutrition_state"] == "wait_manual"
    assert "структурный формат" in message.message.outbound[-1][0]
    day = store.get_own_day(client_telegram_id=CLIENT_ID, local_date=ctl._today(CLIENT_ID))
    assert day["meals"] == []


def test_clarification_failure_retains_prior_context_for_retry(store):
    ai = FakeAI(error=NutritionAIUnavailable("temporary"))
    ctl = controller(store, ai)
    ctl._ensure_user(make_update())
    context = make_context(
        state={
            "nutrition_active": True,
            "nutrition_state": "clarify_ai",
            "nutrition_pending_description": "гречка и курица",
            "nutrition_pending_items": [{
                "name": "Гречка", "weight_g": 180, "portion_text": "",
                "calories": 190, "protein_g": 6, "fat_g": 2, "carbs_g": 38,
                "approximate": True,
            }],
            "nutrition_pending_source": "photo",
            "nutrition_pending_photo_file_id": "file-1",
        }
    )

    assert run(ctl.handle_text(make_update(text="порция 300 г"), context)) is True
    assert context.user_data["nutrition_pending_description"] == "гречка и курица"
    assert context.user_data["nutrition_pending_source"] == "photo"
    assert context.user_data["nutrition_pending_photo_file_id"] == "file-1"
    assert len(ai.clarification_calls) == 1
    assert ai.clarification_calls[0][0] == "порция 300 г"
    assert ai.clarification_calls[-1][0] == "порция 300 г"


def test_telegram_file_error_switches_to_manual_without_propagating(store):
    ctl = controller(store, FakeAI())
    ctl._ensure_user(make_update())
    store.set_photo_consent(telegram_id=CLIENT_ID, version=PHOTO_CONSENT_VERSION)
    context = make_context(
        state={"nutrition_active": True, "nutrition_state": "wait_photo"},
        bot=FakeBot(error=TelegramError("download failed")),
    )
    update = make_update(photo_file_id="file-1")

    assert run(ctl.handle_photo(update, context)) is True
    assert context.user_data["nutrition_state"] == "manual_item"
    assert "Название продукта" in update.message.outbound[-1][0]


def test_water_callback_is_idempotent_by_callback_id(store):
    ctl = controller(store)
    context = make_context(state={"nutrition_active": True})
    ctl._ensure_user(make_update())
    for _ in range(2):
        update = make_update(
            callback_data="nutrition:water:250", callback_id="same-callback"
        )
        assert run(ctl.handle_callback(update, context)) is True
    day = store.get_own_day(client_telegram_id=CLIENT_ID, local_date=ctl._today(CLIENT_ID))
    assert day["water_ml"] == 250


def test_trainer_can_set_norms_for_linked_client_but_client_cannot(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    trainer_update = make_update(user_id=TRAINER_ID)
    client_update = make_update(user_id=CLIENT_ID)
    ctl._ensure_user(trainer_update)
    ctl._ensure_user(client_update)
    invite = store.create_trainer_invite(trainer_telegram_id=TRAINER_ID)
    store.link_client_by_code(client_telegram_id=CLIENT_ID, code=invite["code"])
    client = store.get_user_by_telegram_id(CLIENT_ID)

    trainer_context = make_context(state={"nutrition_active": True})
    open_form = make_update(
        user_id=TRAINER_ID,
        callback_data=f"nutrition:trainer_norms:{client['id']}",
    )
    assert run(ctl.handle_callback(open_form, trainer_context)) is True
    date_markup = open_form.callback_query.edits[-1][1]["reply_markup"]
    other_date = next(
        value for value in nutrition_callbacks(date_markup)
        if value.startswith("nutrition:norms_date:") and value.endswith(":other")
    )
    choose_date = make_update(
        user_id=TRAINER_ID, callback_data=other_date
    )
    run(ctl.handle_callback(choose_date, trainer_context))
    run(ctl.handle_text(make_update(user_id=TRAINER_ID, text="2033-05-18"), trainer_context))
    for value in ("2000", "120", "70", "240", "2200"):
        assert run(ctl.handle_text(make_update(user_id=TRAINER_ID, text=value), trainer_context)) is True
    assert store.get_client_day(TRAINER_ID, client["id"], "2033-05-18")["norms"] is None
    confirm_callback = (
        f"nutrition:norms_confirm:{client['id']}:"
        f"{trainer_context.user_data['nutrition_norms_nonce']}"
    )
    confirm = make_update(user_id=TRAINER_ID, callback_data=confirm_callback)
    run(ctl.handle_callback(confirm, trainer_context))
    day = store.get_client_day(TRAINER_ID, client["id"], "2033-05-18")
    assert day["norms"]["calories"] == 2000

    client_context = make_context(state={"nutrition_active": True})
    denied = make_update(
        user_id=CLIENT_ID,
        callback_data=f"nutrition:trainer_norms:{client['id']}",
    )
    assert run(ctl.handle_callback(denied, client_context)) is True
    assert "только тренеру" in denied.callback_query.message.outbound[-1][0]


def load_root_with_fakes(monkeypatch, dashboard_factory, store_type=None):
    class Filter:
        def __and__(self, other):
            return self

        def __invert__(self):
            return self

    class Handler:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class ApplicationInstance:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler):
            self.handlers.append(handler)

    class ApplicationBuilder:
        def token(self, value):
            self.token_value = value
            return self

        def post_init(self, callback):
            self.post_init_callback = callback
            return self

        def post_shutdown(self, callback):
            self.post_shutdown_callback = callback
            return self

        def build(self):
            return ApplicationInstance()

    class Application:
        @staticmethod
        def builder():
            return ApplicationBuilder()

    class Keyboard:
        def __init__(self, keyboard, **kwargs):
            self.keyboard = keyboard
            self.kwargs = kwargs

    class KeyboardButton:
        def __init__(self, text):
            self.text = text

    class RootStore:
        def __init__(self, path):
            self.path = path

        def list_trainer_clients(self, trainer_id):
            return []

    StoreType = store_type or RootStore

    telegram = sys.modules["telegram"]
    telegram_ext = sys.modules["telegram.ext"]
    monkeypatch.setattr(telegram, "ReplyKeyboardMarkup", Keyboard, raising=False)
    monkeypatch.setattr(telegram, "KeyboardButton", KeyboardButton, raising=False)
    monkeypatch.setattr(telegram_ext, "Application", Application, raising=False)
    monkeypatch.setattr(telegram_ext, "CommandHandler", Handler, raising=False)
    monkeypatch.setattr(telegram_ext, "CallbackQueryHandler", Handler, raising=False)
    monkeypatch.setattr(telegram_ext, "MessageHandler", Handler, raising=False)
    monkeypatch.setattr(
        telegram_ext,
        "filters",
        SimpleNamespace(
            PHOTO=Filter(), TEXT=Filter(), COMMAND=Filter(),
            Document=SimpleNamespace(ALL=Filter()),
        ),
        raising=False,
    )

    modules = {
        "src.bot": SimpleNamespace(NatriumBot=type("NatriumBot", (), {})),
        "src.openai_bot": SimpleNamespace(OpenAIBot=type("OpenAIBot", (), {})),
        "src.nutrition_ai": SimpleNamespace(NutritionAI=type("NutritionAI", (), {})),
        "src.nutrition_bot": nutrition_bot_module,
        "src.nutrition_dashboard": SimpleNamespace(create_dashboard_from_env=dashboard_factory),
        "src.nutrition_store": SimpleNamespace(NutritionStore=StoreType),
        "src.config": SimpleNamespace(TELEGRAM_BOT_TOKEN="123456:test"),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    path = Path(__file__).parents[1] / "src" / "telegram_bot.py"
    spec = importlib.util.spec_from_file_location("nutrition_root_behavior_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_dashboard_flag_and_start_failure_never_expose_broken_web_button(monkeypatch):
    monkeypatch.setenv("TRAINER_TELEGRAM_IDS", str(TRAINER_ID))
    monkeypatch.setenv("NUTRITION_DASHBOARD_ORIGIN", "https://example.invalid")
    monkeypatch.setenv("NUTRITION_DASHBOARD_ENABLED", "0")
    dashboard_calls = []

    def disabled_factory(*args, **kwargs):
        dashboard_calls.append((args, kwargs))
        raise AssertionError("disabled dashboard must not be created")

    root = load_root_with_fakes(monkeypatch, disabled_factory)
    bot = root.TelegramSMMBot()
    assert bot.application is not None
    registered_commands = {
        handler.args[0]
        for handler in bot.application.handlers
        if handler.args and isinstance(handler.args[0], str)
    }
    assert {"help", "nutrition", "today", "week"} <= registered_commands
    assert dashboard_calls == []
    assert bot.nutrition_dashboard is None
    assert bot.nutrition.dashboard_origin == ""
    trainer_update = make_update(user_id=TRAINER_ID, callback_data="nutrition:trainer")
    run(bot.nutrition._show_trainer_clients(trainer_update))
    assert "постоянный код" in trainer_update.callback_query.edits[-1][0]
    markup = trainer_update.callback_query.edits[-1][1]["reply_markup"]
    assert all(button.web_app is None for row in markup.inline_keyboard for button in row)

    class FailingDashboard:
        def start(self):
            raise OSError("bind denied")

        def stop(self):
            pass

    monkeypatch.setenv("NUTRITION_DASHBOARD_ENABLED", "1")
    root = load_root_with_fakes(monkeypatch, lambda *args, **kwargs: FailingDashboard())
    bot = root.TelegramSMMBot()
    assert bot.nutrition.dashboard_origin == "https://example.invalid"
    run(bot._post_init(bot.application))
    assert bot.application is not None
    assert bot.nutrition_dashboard is None
    assert bot.nutrition.dashboard_origin == ""

    class FailingStore:
        def __init__(self, path):
            raise OSError("database unavailable")

    monkeypatch.setenv("NUTRITION_DASHBOARD_ENABLED", "0")
    root = load_root_with_fakes(monkeypatch, disabled_factory, store_type=FailingStore)
    bot = root.TelegramSMMBot()
    assert bot.application is not None
    assert bot.nutrition is None
    assert bot.nutrition_dashboard is None

    class StopFailingDashboard:
        def stop(self):
            raise OSError("shutdown failed")

    bot.nutrition_dashboard = StopFailingDashboard()
    run(bot._post_shutdown(bot.application))
    assert bot.application is not None


def _linked_confirmed_meal(store, ctl):
    ctl._ensure_user(make_update(user_id=TRAINER_ID))
    ctl._ensure_user(make_update(user_id=CLIENT_ID))
    invite = store.create_trainer_invite(trainer_telegram_id=TRAINER_ID)
    store.link_client_by_code(client_telegram_id=CLIENT_ID, code=invite["code"])
    meal = store.create_meal_draft(
        client_telegram_id=CLIENT_ID,
        source="manual",
        eaten_at="2026-09-08T08:30:00+03:00",
        meal_type="завтрак",
        items=[{
            "name": "Омлет", "weight_g": 200, "portion_text": "",
            "calories": 300, "protein_g": 20, "fat_g": 22, "carbs_g": 4,
            "approximate": False,
        }],
    )
    return store.confirm_meal(
        client_telegram_id=CLIENT_ID, meal_id=meal["id"], idempotency_key="trainer-ux"
    )


def test_trainer_comment_flow_uses_named_meal_plain_text_and_navigation(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    meal = _linked_confirmed_meal(store, ctl)
    client = store.get_user_by_telegram_id(CLIENT_ID)
    context = make_context(state={"nutrition_active": True})

    choose_action = make_update(
        user_id=TRAINER_ID, callback_data=f"nutrition:trainer_comment:{client['id']}"
    )
    run(ctl.handle_callback(choose_action, context))
    picker = choose_action.callback_query.edits[-1][1]["reply_markup"]
    meal_callback = next(value for value in nutrition_callbacks(picker) if value.startswith("nutrition:tcm:"))
    assert "Омлет" in picker.inline_keyboard[0][0].text
    assert "ID" not in choose_action.callback_query.edits[-1][0]

    choose_meal = make_update(user_id=TRAINER_ID, callback_data=meal_callback)
    run(ctl.handle_callback(choose_meal, context))
    assert context.user_data["nutrition_state"] == "trainer_comment_text"
    assert "обычным текстом" in choose_meal.callback_query.edits[-1][0]
    callbacks = nutrition_callbacks(choose_meal.callback_query.edits[-1][1]["reply_markup"])
    assert any(value.startswith("nutrition:trainer_comment:") for value in callbacks)

    comment = make_update(user_id=TRAINER_ID, text="Ivan Solovyev; Молодец, хороший прием пищи")
    run(ctl.handle_text(comment, context))
    assert "Комментарий добавлен" in comment.message.outbound[-1][0]
    assert {"nutrition_active", "nutrition_trainer_meal_id", "nutrition_trainer_client_id"} <= set(context.user_data)
    stored = store.get_client_meal(TRAINER_ID, meal["id"])
    assert stored["comments"][-1]["text"] == "Ivan Solovyev; Молодец, хороший прием пищи"

    context.user_data["nutrition_state"] = "trainer_comment_text"
    context.user_data["nutrition_pending_plan"] = [{"stale": True}]
    run(ctl.start(make_update(user_id=TRAINER_ID), context))
    assert context.user_data == {"nutrition_active": True}


def test_client_reads_latest_trainer_comments_without_internal_ids(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    meal = _linked_confirmed_meal(store, ctl)
    store.add_comment(TRAINER_ID, meal["id"], "Добавьте овощи к обеду")
    context = make_context(state={"nutrition_active": True})

    opened = make_update(callback_data="nutrition:comments")
    run(ctl.handle_callback(opened, context))

    text, kwargs = opened.callback_query.edits[-1]
    assert "Добавьте овощи к обеду" in text
    assert "Тренер" in text
    assert "завтрак" in text
    assert str(TRAINER_ID) not in text
    assert "nutrition:menu" in nutrition_callbacks(kwargs["reply_markup"])


def test_repeat_picker_deduplicates_double_tap_and_new_picker_allows_new_draft(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    original = _linked_confirmed_meal(store, ctl)
    context = make_context(state={"nutrition_active": True})

    picker = make_update(callback_data="nutrition:repeat")
    run(ctl.handle_callback(picker, context))
    repeat_callback = next(
        value
        for value in nutrition_callbacks(picker.callback_query.edits[-1][1]["reply_markup"])
        if value.startswith("nutrition:repeat_meal:")
    )
    first_draft_id = None
    for callback_id in ("repeat-tap-1", "repeat-tap-2"):
        repeated = make_update(callback_data=repeat_callback, callback_id=callback_id)
        run(ctl.handle_callback(repeated, context))
        current_id = context.user_data["nutrition_draft_meal_id"]
        first_draft_id = first_draft_id or current_id
        assert current_id == first_draft_id

    draft_id = context.user_data["nutrition_draft_meal_id"]
    draft = store.get_owned_draft(client_telegram_id=CLIENT_ID, meal_id=draft_id)
    assert draft_id != original["id"]
    assert draft["status"] == "draft"
    assert [item["name"] for item in draft["items"]] == ["Омлет"]
    assert draft["comments"] == []
    text, kwargs = repeated.message.outbound[-1]
    assert "новый неподтвержденный черновик" in text
    callbacks = nutrition_callbacks(kwargs["reply_markup"])
    assert f"nutrition:edit:{draft_id}" in callbacks
    assert f"nutrition:meal_context:{draft_id}" in callbacks
    recent = store.list_own_recent_meals(client_telegram_id=CLIENT_ID, limit=10)
    assert [meal["id"] for meal in recent] == [original["id"]]

    second_picker = make_update(callback_data="nutrition:repeat")
    run(ctl.handle_callback(second_picker, context))
    second_callback = next(
        value
        for value in nutrition_callbacks(second_picker.callback_query.edits[-1][1]["reply_markup"])
        if value.startswith("nutrition:repeat_meal:")
    )
    assert second_callback != repeat_callback
    run(ctl.handle_callback(make_update(callback_data=second_callback), context))
    assert context.user_data["nutrition_draft_meal_id"] != first_draft_id


def test_stale_trainer_meal_callback_is_denied_after_unlink(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    _linked_confirmed_meal(store, ctl)
    client = store.get_user_by_telegram_id(CLIENT_ID)
    context = make_context(state={"nutrition_active": True})
    picker_update = make_update(
        user_id=TRAINER_ID, callback_data=f"nutrition:trainer_comment:{client['id']}"
    )
    run(ctl.handle_callback(picker_update, context))
    callback = next(
        value for value in nutrition_callbacks(picker_update.callback_query.edits[-1][1]["reply_markup"])
        if value.startswith("nutrition:tcm:")
    )
    store.unlink_client(trainer_telegram_id=TRAINER_ID, client_id=client["id"])
    stale = make_update(user_id=TRAINER_ID, callback_data=callback)
    run(ctl.handle_callback(stale, context))
    assert "не привязан" in stale.callback_query.message.outbound[-1][0]


def test_reference_flow_uses_stable_fdc_ids_and_builds_multiple_products(store):
    ctl = controller(store)
    assert ctl.reference is not None
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(make_update(callback_data="nutrition:meal_reference"), context))

    first_search = make_update(text="banana raw")
    run(ctl.handle_text(first_search, context))
    first_markup = first_search.message.outbound[-1][1]["reply_markup"]
    first_pick = next(value for value in nutrition_callbacks(first_markup) if value.startswith("nutrition:ref_pick:"))
    assert first_pick.rsplit(":", 1)[1].isdigit()
    grams_prompt = make_update(callback_data=first_pick)
    run(ctl.handle_callback(grams_prompt, context))
    grams_callbacks = nutrition_callbacks(grams_prompt.callback_query.edits[-1][1]["reply_markup"])
    assert "nutrition:ref_back_candidates" in grams_callbacks
    run(ctl.handle_text(make_update(text="120"), context))

    more = make_update(callback_data="nutrition:ref_more")
    run(ctl.handle_callback(more, context))
    assert "nutrition:ref_review" in nutrition_callbacks(more.callback_query.edits[-1][1]["reply_markup"])
    second_search = make_update(text="rice cooked")
    run(ctl.handle_text(second_search, context))
    second_pick = next(
        value for value in nutrition_callbacks(second_search.message.outbound[-1][1]["reply_markup"])
        if value.startswith("nutrition:ref_pick:")
    )
    run(ctl.handle_callback(make_update(callback_data=second_pick), context))
    run(ctl.handle_text(make_update(text="180"), context))

    finish = make_update(callback_data="nutrition:ref_finish")
    run(ctl.handle_callback(finish, context))
    draft_text = finish.callback_query.message.outbound[-1][0]
    meal_id = int(re.search(r"#(\d+)", draft_text).group(1))
    meal = store.get_owned_draft(client_telegram_id=CLIENT_ID, meal_id=meal_id)
    assert len(meal["items"]) == 2
    assert all(item["calculation_method"] == "reference" for item in meal["items"])
    assert len({item["reference_fdc_id"] for item in meal["items"]}) == 2


def test_xlsx_preview_has_no_write_and_confirm_rechecks_access(store, monkeypatch):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    _linked_confirmed_meal(store, ctl)
    client = store.get_user_by_telegram_id(CLIENT_ID)
    context = make_context(state={"nutrition_active": True})
    open_upload = make_update(
        user_id=TRAINER_ID, callback_data=f"nutrition:trainer_plan_upload:{client['id']}"
    )
    run(ctl.handle_callback(open_upload, context))
    monkeypatch.setattr(
        nutrition_bot_module,
        "parse_plan",
        lambda payload: [SimpleNamespace(
            effective_from=date(2033, 5, 18), calories=2000, protein_g=120,
            fat_g=70, carbs_g=240, water_ml=2200,
        )],
    )
    document = SimpleNamespace(file_name="plan.xlsx", file_size=100, file_id="xlsx-file")
    upload = make_update(user_id=TRAINER_ID, document=document)
    run(ctl.handle_document(upload, context))
    assert context.user_data["nutrition_state"] == "trainer_plan_preview"
    assert store.get_client_day(TRAINER_ID, client["id"], "2033-05-18")["norms"] is None

    store.unlink_client(trainer_telegram_id=TRAINER_ID, client_id=client["id"])
    confirm_callback = next(
        value for value in nutrition_callbacks(upload.message.outbound[-1][1]["reply_markup"])
        if value.startswith("nutrition:trainer_plan_confirm:")
    )
    confirm = make_update(user_id=TRAINER_ID, callback_data=confirm_callback)
    run(ctl.handle_callback(confirm, context))
    assert "не привязан" in confirm.callback_query.message.outbound[-1][0]


def test_reference_no_match_returns_to_current_work_and_revokes_old_candidates(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    valid = ctl.reference.search("banana raw", limit=1)[0]
    context = make_context(state={
        "nutrition_active": True,
        "nutrition_state": "reference_search",
        "nutrition_reference_mode": "build",
        "nutrition_reference_items": [{"name": "already selected"}],
        "nutrition_reference_candidates": [valid.fdc_id],
    })

    no_match = make_update(text="творог 9%")
    run(ctl.handle_text(no_match, context))
    callbacks = nutrition_callbacks(no_match.message.outbound[-1][1]["reply_markup"])
    assert "nutrition:ref_review" in callbacks
    assert "nutrition_reference_candidates" not in context.user_data

    stale = make_update(callback_data=f"nutrition:ref_pick:{valid.fdc_id}")
    run(ctl.handle_callback(stale, context))
    assert "уже недоступен" in stale.callback_query.message.outbound[-1][0]

    context.user_data.update({
        "nutrition_reference_mode": "refine",
        "nutrition_reference_meal_id": 77,
        "nutrition_reference_candidates": [valid.fdc_id],
    })
    refine_no_match = make_update(text="творог 9%")
    run(ctl.handle_text(refine_no_match, context))
    refine_callbacks = nutrition_callbacks(
        refine_no_match.message.outbound[-1][1]["reply_markup"]
    )
    assert "nutrition:refine:77" in refine_callbacks
    assert "nutrition_reference_candidates" not in context.user_data


def test_day_summary_shows_all_norm_units_and_distinguishes_zero_from_none():
    summary = {
        "client": {"display_name": "Клиент"},
        "date": "2033-05-18",
        "meals": [],
        "totals": {"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0},
        "water_ml": 0,
        "norms": {
            "effective_from": "2033-05-18",
            "calories": 0,
            "protein_g": None,
            "fat_g": 0,
            "carbs_g": 240,
            "water_ml": None,
        },
    }

    text = NutritionBotController._format_day(summary)
    assert "0 ккал/сутки" in text
    assert "Б не задано г/сутки" in text
    assert "Ж 0 г/сутки" in text
    assert "У 240 г/сутки" in text
    assert "вода не задано мл/сутки" in text


def test_start_help_and_diary_button_interrupt_pending_nutrition_input(monkeypatch):
    monkeypatch.setenv("NUTRITION_DASHBOARD_ENABLED", "0")
    root = load_root_with_fakes(monkeypatch, lambda *args, **kwargs: None)
    bot = root.TelegramSMMBot()

    class ResetNutrition:
        @staticmethod
        def reset(context):
            for key in list(context.user_data):
                if key.startswith("nutrition_"):
                    context.user_data.pop(key)

        @staticmethod
        def is_active(context):
            return bool(context.user_data.get("nutrition_active"))

        async def start(self, update, context):
            self.reset(context)
            context.user_data["nutrition_active"] = True
            await update.effective_message.reply_text("Дневник")

    bot.nutrition = ResetNutrition()
    pending = {
        "nutrition_active": True,
        "nutrition_state": "trainer_comment_text",
        "nutrition_trainer_meal_id": 77,
    }

    start_context = make_context(state=pending)
    run(bot.start_command(make_update(text="/start"), start_context))
    assert not any(key.startswith("nutrition_") for key in start_context.user_data)

    help_context = make_context(state=pending)
    run(bot.help_command(make_update(text="/help"), help_context))
    assert help_context.user_data == {"nutrition_active": True}

    diary_context = make_context(state=pending)
    run(bot.text_handler(make_update(text="🥗 Дневник питания"), diary_context))
    assert diary_context.user_data == {"nutrition_active": True}


def test_norms_back_keeps_entered_value_and_date(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    _linked_confirmed_meal(store, ctl)
    client = store.get_user_by_telegram_id(CLIENT_ID)
    context = make_context(state={"nutrition_active": True})
    opened = make_update(
        user_id=TRAINER_ID, callback_data=f"nutrition:trainer_norms:{client['id']}"
    )
    run(ctl.handle_callback(opened, context))
    today_callback = next(
        value for value in nutrition_callbacks(opened.callback_query.edits[-1][1]["reply_markup"])
        if value.startswith("nutrition:norms_date:") and value.endswith(":today")
    )
    run(ctl.handle_callback(make_update(user_id=TRAINER_ID, callback_data=today_callback), context))
    protein_prompt = make_update(user_id=TRAINER_ID, text="2000")
    run(ctl.handle_text(protein_prompt, context))

    back_to_calories = next(
        value for value in nutrition_callbacks(protein_prompt.message.outbound[-1][1]["reply_markup"])
        if value.startswith("nutrition:norms_back:")
    )
    calories_again = make_update(user_id=TRAINER_ID, callback_data=back_to_calories)
    run(ctl.handle_callback(calories_again, context))
    markup = calories_again.callback_query.message.outbound[-1][1]["reply_markup"]
    entered = next(
        button for row in markup.inline_keyboard for button in row
        if button.callback_data and button.callback_data.endswith(":keep_draft")
    )
    assert "2000" in entered.text

    back_to_date = next(
        value for value in nutrition_callbacks(markup)
        if value.startswith("nutrition:norms_back:")
    )
    date_again = make_update(user_id=TRAINER_ID, callback_data=back_to_date)
    run(ctl.handle_callback(date_again, context))
    assert "Сейчас выбрано:" in date_again.callback_query.edits[-1][0]
    assert context.user_data["nutrition_norms_draft"]["calories"] == 2000


def test_stale_norms_confirm_from_another_client_writes_nothing(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    ctl._ensure_user(make_update(user_id=TRAINER_ID))
    client_ids = []
    for telegram_id in (3101, 3102):
        client = store.ensure_user(telegram_id=telegram_id, display_name=f"Client {telegram_id}")
        code = store.create_trainer_invite(trainer_telegram_id=TRAINER_ID)["code"]
        store.link_client_by_code(client_telegram_id=telegram_id, code=code)
        client_ids.append(client["id"])
    context = make_context(state={"nutrition_active": True})

    def build_preview(client_id, base):
        opened = make_update(
            user_id=TRAINER_ID, callback_data=f"nutrition:trainer_norms:{client_id}"
        )
        run(ctl.handle_callback(opened, context))
        today_callback = next(
            value for value in nutrition_callbacks(opened.callback_query.edits[-1][1]["reply_markup"])
            if value.startswith("nutrition:norms_date:") and value.endswith(":today")
        )
        run(ctl.handle_callback(
            make_update(user_id=TRAINER_ID, callback_data=today_callback), context
        ))
        prompt = None
        for value in (str(base), "120", "70", "240", "2200"):
            prompt = make_update(user_id=TRAINER_ID, text=value)
            run(ctl.handle_text(prompt, context))
        return next(
            value for value in nutrition_callbacks(prompt.message.outbound[-1][1]["reply_markup"])
            if value.startswith("nutrition:norms_confirm:")
        )

    old_confirm = build_preview(client_ids[0], 2000)
    build_preview(client_ids[1], 2100)
    stale = make_update(user_id=TRAINER_ID, callback_data=old_confirm)
    run(ctl.handle_callback(stale, context))
    assert "устарел" in stale.callback_query.message.outbound[-1][0]
    for client_id in client_ids:
        today = ctl._today_for_client(TRAINER_ID, client_id)
        assert store.get_client_day(TRAINER_ID, client_id, today)["norms"] is None


def test_stale_xlsx_confirm_from_another_client_writes_nothing(store, monkeypatch):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    ctl._ensure_user(make_update(user_id=TRAINER_ID))
    client_ids = []
    for telegram_id in (3201, 3202):
        client = store.ensure_user(telegram_id=telegram_id, display_name=f"Client {telegram_id}")
        code = store.create_trainer_invite(trainer_telegram_id=TRAINER_ID)["code"]
        store.link_client_by_code(client_telegram_id=telegram_id, code=code)
        client_ids.append(client["id"])
    monkeypatch.setattr(
        nutrition_bot_module,
        "parse_plan",
        lambda payload: [SimpleNamespace(
            effective_from=date(2033, 5, 18), calories=2000, protein_g=120,
            fat_g=70, carbs_g=240, water_ml=2200,
        )],
    )
    context = make_context(state={"nutrition_active": True})

    def upload_preview(client_id):
        opened = make_update(
            user_id=TRAINER_ID,
            callback_data=f"nutrition:trainer_plan_upload:{client_id}",
        )
        run(ctl.handle_callback(opened, context))
        document = SimpleNamespace(file_name="plan.xlsx", file_size=100, file_id="xlsx-file")
        upload = make_update(user_id=TRAINER_ID, document=document)
        run(ctl.handle_document(upload, context))
        return next(
            value for value in nutrition_callbacks(upload.message.outbound[-1][1]["reply_markup"])
            if value.startswith("nutrition:trainer_plan_confirm:")
        )

    old_confirm = upload_preview(client_ids[0])
    upload_preview(client_ids[1])
    stale = make_update(user_id=TRAINER_ID, callback_data=old_confirm)
    run(ctl.handle_callback(stale, context))
    assert "устарел" in stale.callback_query.message.outbound[-1][0]
    for client_id in client_ids:
        assert store.get_client_day(TRAINER_ID, client_id, "2033-05-18")["norms"] is None


class FakeHelpService:
    timeout_seconds = 0.1

    def __init__(self, result="Ответ по всем пунктам."):
        self.calls = []
        self.result = result

    def answer(self, question, help_context, *, history=None):
        self.calls.append((question, help_context, list(history or [])))
        return self.result

    def fallback_answer(self, question, help_context):
        return "Встроенная справка."


def test_long_help_answer_is_split_and_keyboard_is_only_on_last_message(store):
    result = "\n\n".join(f"{index}. Пункт справки: {'текст ' * 700}" for index in range(1, 10))
    helper = FakeHelpService(result)
    ctl = controller(store, help_service=helper)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})

    question = make_update(text="Что вы умеете?")
    run(ctl.handle_text(question, context))

    messages = question.message.outbound
    assert len(messages) > 1
    assert all(len(text) <= 3900 for text, _ in messages)
    assert all("reply_markup" not in kwargs for _, kwargs in messages[:-1])
    assert "nutrition:help_resume" in nutrition_callbacks(messages[-1][1]["reply_markup"])
    assert "1. Пункт справки" in "\n\n".join(text for text, _ in messages)
    assert "9. Пункт справки" in "\n\n".join(text for text, _ in messages)
    assert context.user_data["nutrition_state"] == "nutrition_help_question"


def test_help_split_escapes_complete_fragments_and_counts_utf16_units(store):
    result = "x" * 3898 + "😀<>&\"'" + "\n\nхвост"
    helper = FakeHelpService(result)
    ctl = controller(store, help_service=helper)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})

    question = make_update(text="Что вы умеете?")
    run(ctl.handle_text(question, context))

    sent = [text for text, _ in question.message.outbound]
    escaped = "".join(sent)
    assert "&lt;" in escaped and "&gt;" in escaped and "&amp;" in escaped
    assert "&quot;" in escaped and "&#x27;" in escaped
    assert sum(text.count("😀") for text in sent) == 1
    assert "".join(html.unescape(text) for text in sent) == result
    assert all(len(html.unescape(text).encode("utf-16-le")) // 2 <= 3900 for text in sent)


def test_natural_help_from_menu_and_manual_field_preserves_input_state(store):
    helper = FakeHelpService()
    ctl = controller(store, help_service=helper)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})

    menu_question = make_update(text="Что вы умеете?")
    run(ctl.handle_text(menu_question, context))
    assert helper.calls[-1][0] == "Что вы умеете?"
    assert helper.calls[-1][1].screen == "Меню дневника"
    assert helper.calls[-1][2] == []
    followup = make_update(text="А где это открыть?")
    run(ctl.handle_text(followup, context))
    assert helper.calls[-1][2] == [{
        "question": "Что вы умеете?",
        "answer": "Ответ по всем пунктам.",
    }]
    run(ctl.handle_callback(make_update(callback_data="nutrition:help_resume"), context))
    assert "nutrition_help_history" not in context.user_data

    run(ctl.handle_callback(make_update(callback_data="nutrition:meal_manual"), context))
    run(ctl.handle_text(make_update(text="Рис"), context))
    nonce = context.user_data["nutrition_manual_nonce"]
    invalid = make_update(text="сто восемьдесят")
    run(ctl.handle_text(invalid, context))
    assert "введите число" in invalid.message.outbound[-1][0]
    assert context.user_data["nutrition_manual_step"] == 1

    field_question = make_update(text="Зачем нужна масса всей порции?")
    run(ctl.handle_text(field_question, context))
    assert helper.calls[-1][0] == "Зачем нужна масса всей порции?"
    assert helper.calls[-1][2] == []
    saved = context.user_data["nutrition_help_return"]
    assert saved["nutrition_manual_nonce"] == nonce
    assert saved["nutrition_manual_step"] == 1
    assert saved["nutrition_manual_item"] == {"name": "Рис"}

    run(ctl.handle_callback(make_update(callback_data="nutrition:help_resume"), context))
    assert context.user_data["nutrition_manual_nonce"] == nonce
    assert context.user_data["nutrition_manual_step"] == 1
    assert context.user_data["nutrition_manual_item"] == {"name": "Рис"}


def test_help_followup_uses_bounded_sanitized_history_and_clears_on_context_change(store):
    helper = FakeHelpService(result="На каком экране вы находитесь?")
    ctl = controller(store, help_service=helper)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})

    run(ctl.handle_callback(make_update(callback_data="nutrition:help"), context))
    run(ctl.handle_text(
            make_update(text="Мой рацион: овсянка\nTelegram ID 123456789. Почему значение другое?"),
        context,
    ))
    helper.result = "На экране недели учитываются подтвержденные записи."
    run(ctl.handle_text(make_update(text="Я на экране недели"), context))

    assert len(helper.calls) == 2
    history = helper.calls[-1][2]
    assert len(history) == 1
    serialized = repr(history)
    assert "123456789" not in serialized
    assert "овсянка" not in serialized.casefold()
    assert "[ID скрыт]" in serialized
    assert "[рацион скрыт]" in serialized

    run(ctl.handle_callback(make_update(callback_data="nutrition:menu"), context))
    assert "nutrition_help_history" not in context.user_data
    run(ctl.handle_callback(make_update(callback_data="nutrition:meal_manual"), context))
    run(ctl.handle_text(make_update(text="Что сюда писать?"), context))
    assert helper.calls[-1][1].state == "manual_item"
    assert helper.calls[-1][2] == []


def test_help_inside_norms_preserves_draft_step_and_nonce(store):
    helper = FakeHelpService()
    ctl = controller(store, trainer_ids={TRAINER_ID}, help_service=helper)
    _linked_confirmed_meal(store, ctl)
    client = store.get_user_by_telegram_id(CLIENT_ID)
    context = make_context(state={"nutrition_active": True})
    opened = make_update(
        user_id=TRAINER_ID, callback_data=f"nutrition:trainer_norms:{client['id']}"
    )
    run(ctl.handle_callback(opened, context))
    nonce = context.user_data["nutrition_norms_nonce"]
    context.user_data.update({
        "nutrition_state": "norms_value",
        "nutrition_norms_step": 1,
        "nutrition_norms_draft": {"effective_from": "2033-05-18", "calories": 2000.0},
    })

    question = make_update(
        user_id=TRAINER_ID,
        text="Это граммы или проценты? Как вернуться к калориям?",
    )
    run(ctl.handle_text(question, context))

    assert context.user_data["nutrition_state"] == "nutrition_help_question"
    saved = context.user_data["nutrition_help_return"]
    assert saved["nutrition_norms_step"] == 1
    assert saved["nutrition_norms_nonce"] == nonce
    assert saved["nutrition_norms_draft"] == {
        "effective_from": "2033-05-18", "calories": 2000.0,
    }
    assert len(helper.calls) == 1
    assert helper.calls[0][1].role == "trainer"
    assert helper.calls[0][1].screen == "Нормы: Белки, г/сутки"
    assert str(client["id"]) not in repr(helper.calls[0][1])

    resume = make_update(user_id=TRAINER_ID, callback_data="nutrition:help_resume")
    run(ctl.handle_callback(resume, context))
    assert context.user_data["nutrition_state"] == "norms_value"
    assert context.user_data["nutrition_norms_step"] == 1
    assert context.user_data["nutrition_norms_nonce"] == nonce
    assert context.user_data["nutrition_norms_draft"]["calories"] == 2000.0
    assert "г/сутки" in resume.callback_query.message.outbound[-1][0]


def test_help_question_in_comment_is_not_saved_and_plain_question_comment_is_saved(store):
    helper = FakeHelpService()
    ctl = controller(store, trainer_ids={TRAINER_ID}, help_service=helper)
    meal = _linked_confirmed_meal(store, ctl)
    client = store.get_user_by_telegram_id(CLIENT_ID)
    context = make_context(state={
        "nutrition_active": True,
        "nutrition_state": "trainer_comment_text",
        "nutrition_trainer_meal_id": meal["id"],
        "nutrition_trainer_client_id": client["id"],
    })

    help_question = make_update(
        user_id=TRAINER_ID, text="Как правильно направить комментарий?"
    )
    run(ctl.handle_text(help_question, context))
    assert context.user_data["nutrition_state"] == "nutrition_help_question"
    assert store.get_client_meal(TRAINER_ID, meal["id"])["comments"] == []
    assert len(helper.calls) == 1

    run(ctl.handle_callback(
        make_update(user_id=TRAINER_ID, callback_data="nutrition:help_resume"), context
    ))
    plain_comment = make_update(user_id=TRAINER_ID, text="Как прошла тренировка?")
    run(ctl.handle_text(plain_comment, context))
    stored = store.get_client_meal(TRAINER_ID, meal["id"])
    assert stored["comments"][-1]["text"] == "Как прошла тренировка?"
    assert len(helper.calls) == 1


def test_help_resume_rechecks_trainer_access(store):
    helper = FakeHelpService()
    ctl = controller(store, trainer_ids={TRAINER_ID}, help_service=helper)
    meal = _linked_confirmed_meal(store, ctl)
    client = store.get_user_by_telegram_id(CLIENT_ID)
    context = make_context(state={
        "nutrition_active": True,
        "nutrition_state": "trainer_comment_text",
        "nutrition_trainer_meal_id": meal["id"],
        "nutrition_trainer_client_id": client["id"],
    })
    run(ctl.handle_text(
        make_update(user_id=TRAINER_ID, text="Где в боте выбрать комментарий?"), context
    ))
    store.unlink_client(trainer_telegram_id=TRAINER_ID, client_id=client["id"])

    resume = make_update(user_id=TRAINER_ID, callback_data="nutrition:help_resume")
    run(ctl.handle_callback(resume, context))

    assert "больше недоступен" in resume.callback_query.message.outbound[-1][0]
    assert context.user_data == {"nutrition_active": True}


def test_help_resume_renders_real_water_step_and_stale_help_uses_current_action(store):
    helper = FakeHelpService()
    ctl = controller(store, help_service=helper)
    ctl._ensure_user(make_update())
    context = make_context(state={
        "nutrition_active": True,
        "nutrition_state": "wait_water",
    })

    run(ctl.handle_callback(make_update(callback_data="nutrition:help"), context))
    resume_water = make_update(callback_data="nutrition:help_resume")
    run(ctl.handle_callback(resume_water, context))
    water_text, water_kwargs = resume_water.callback_query.message.outbound[-1]
    assert "целым числом в миллилитрах" in water_text
    assert "nutrition:water" in nutrition_callbacks(water_kwargs["reply_markup"])

    run(ctl.handle_callback(make_update(callback_data="nutrition:help"), context))
    run(ctl.handle_callback(make_update(callback_data="nutrition:meal_manual"), context))
    assert context.user_data["nutrition_state"] == "manual_item"

    resume_manual = make_update(callback_data="nutrition:help_resume")
    run(ctl.handle_callback(resume_manual, context))
    manual_text, manual_kwargs = resume_manual.callback_query.message.outbound[-1]
    assert "Название продукта" in manual_text
    assert "прежнему экрану" in resume_manual.callback_query.message.outbound[-2][0]
    assert "nutrition:add_meal" in nutrition_callbacks(manual_kwargs["reply_markup"])
    assert context.user_data["nutrition_state"] == "manual_item"


def test_stepwise_manual_item_back_help_and_confirm(store):
    helper = FakeHelpService()
    ctl = controller(store, help_service=helper)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(make_update(callback_data="nutrition:meal_manual"), context))
    nonce = context.user_data["nutrition_manual_nonce"]

    run(ctl.handle_text(make_update(text="Рис вареный"), context))
    assert context.user_data["nutrition_manual_step"] == 1
    question = make_update(text="Не понял, это граммы?")
    run(ctl.handle_text(question, context))
    assert context.user_data["nutrition_help_return"]["nutrition_manual_step"] == 1
    assert "Масса всей порции" in helper.calls[-1][1].screen
    run(ctl.handle_callback(make_update(callback_data="nutrition:help_resume"), context))
    assert context.user_data["nutrition_manual_nonce"] == nonce
    assert context.user_data["nutrition_manual_item"]["name"] == "Рис вареный"

    for value in ("180,5", "230,5", "5.4", "0", "48"):
        last = make_update(text=value)
        run(ctl.handle_text(last, context))
    assert context.user_data["nutrition_state"] == "manual_preview"
    back = next(value for value in nutrition_callbacks(last.message.outbound[-1][1]["reply_markup"])
                if value.startswith("nutrition:manual_back:"))
    back_update = make_update(callback_data=back)
    run(ctl.handle_callback(back_update, context))
    assert "Ранее введено: 48" in back_update.callback_query.message.outbound[-1][0]
    run(ctl.handle_text(make_update(text="50"), context))

    confirm = f"nutrition:manual_confirm:{nonce}"
    run(ctl.handle_callback(make_update(callback_data=confirm), context))
    meal_id = context.user_data["nutrition_draft_meal_id"]
    meal = store.get_own_meal(client_telegram_id=CLIENT_ID, meal_id=meal_id)
    assert meal["status"] == "draft"
    assert meal["items"][0]["weight_g"] == 180.5
    assert meal["items"][0]["calories"] == 230.5
    assert meal["items"][0]["carbs_g"] == 50


def test_stale_manual_confirm_cannot_create_after_a_new_manual_flow(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(make_update(callback_data="nutrition:meal_manual"), context))
    old_nonce = context.user_data["nutrition_manual_nonce"]
    for value in ("Рис", "180", "230", "5", "1", "48"):
        run(ctl.handle_text(make_update(text=value), context))
    assert context.user_data["nutrition_state"] == "manual_preview"

    run(ctl.handle_callback(make_update(callback_data="nutrition:meal_manual"), context))
    new_nonce = context.user_data["nutrition_manual_nonce"]
    stale = make_update(callback_data=f"nutrition:manual_confirm:{old_nonce}")
    run(ctl.handle_callback(stale, context))

    assert new_nonce != old_nonce
    assert context.user_data["nutrition_manual_nonce"] == new_nonce
    assert context.user_data["nutrition_state"] == "manual_item"
    today = ctl._today(CLIENT_ID)
    assert store.get_own_day(client_telegram_id=CLIENT_ID, local_date=today)["meals"] == []
    assert "устарел" in stale.callback_query.message.outbound[-1][0]


def test_profile_wizard_and_cancel_are_atomic(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(make_update(callback_data="nutrition:profile_edit"), context))
    original = store.get_profile(telegram_id=CLIENT_ID)
    run(ctl.handle_text(make_update(text="Анна"), context))
    run(ctl.handle_text(make_update(text="172"), context))
    run(ctl.handle_text(make_update(text="Поддерживать режим"), context))
    last = make_update(text="Asia/Tokyo")
    run(ctl.handle_text(last, context))
    assert store.get_profile(telegram_id=CLIENT_ID)["display_name"] == original["display_name"]
    nonce = context.user_data["nutrition_profile_nonce"]
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:profile_confirm:{nonce}"), context))
    profile = store.get_profile(telegram_id=CLIENT_ID)
    assert profile["display_name"] == "Анна"
    assert profile["height_cm"] == 172
    assert profile["goal"] == "Поддерживать режим"
    assert profile["timezone"] == "Asia/Tokyo"

    run(ctl.handle_callback(make_update(callback_data="nutrition:profile_edit"), context))
    run(ctl.handle_text(make_update(text="Не сохранять"), context))
    run(ctl.handle_callback(make_update(callback_data="nutrition:profile"), context))
    assert store.get_profile(telegram_id=CLIENT_ID)["display_name"] == "Анна"


def test_weight_skip_note_save_then_main_menu_clears_wizard(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})

    run(ctl.handle_callback(make_update(callback_data="nutrition:weight_add"), context))
    run(ctl.handle_text(make_update(text="82.5"), context))
    nonce = context.user_data["nutrition_weight_nonce"]
    run(ctl.handle_callback(
        make_update(callback_data=f"nutrition:weight_date:{nonce}:today"), context
    ))
    run(ctl.handle_text(make_update(text="07:30"), context))
    run(ctl.handle_callback(
        make_update(callback_data=f"nutrition:weight_note_skip:{nonce}"), context
    ))
    saved = make_update(callback_data=f"nutrition:weight_confirm:{nonce}")
    run(ctl.handle_callback(saved, context))

    text, kwargs = saved.callback_query.message.outbound[-1]
    assert "Вес 82.5 кг сохранен" in text
    assert "nutrition:menu" in nutrition_callbacks(kwargs["reply_markup"])
    assert context.user_data == {"nutrition_active": True}

    menu = make_update(callback_data="nutrition:menu")
    run(ctl.handle_callback(menu, context))
    assert "Дневник питания" in menu.callback_query.edits[-1][0]
    assert context.user_data == {"nutrition_active": True}


def test_weight_add_history_edit_and_cancel(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(make_update(callback_data="nutrition:weight_add"), context))
    run(ctl.handle_text(make_update(text="82,5"), context))
    nonce = context.user_data["nutrition_weight_nonce"]
    run(ctl.handle_callback(
        make_update(callback_data=f"nutrition:weight_date:{nonce}:today"), context
    ))
    run(ctl.handle_text(make_update(text="07:30"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:weight_back:{nonce}"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:weight_back:{nonce}"), context))
    run(ctl.handle_callback(
        make_update(callback_data=f"nutrition:weight_date:{nonce}:yesterday"), context
    ))
    run(ctl.handle_callback(
        make_update(callback_data=f"nutrition:weight_time_keep:{nonce}"), context
    ))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:weight_note_skip:{nonce}"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:weight_confirm:{nonce}"), context))

    chosen = ctl._today(CLIENT_ID) - timedelta(days=1)
    day = store.get_own_day(client_telegram_id=CLIENT_ID, local_date=chosen)
    row = day["weight_entries"][0]
    local_moment = datetime.fromisoformat(row["measured_at"]).astimezone(ZoneInfo("Europe/Moscow"))
    assert local_moment.date() == chosen
    assert local_moment.strftime("%H:%M") == "07:30"
    edit = f"nutrition:weight_edit:{chosen.isoformat()}:{row['id']}:{row['version']}"
    run(ctl.handle_callback(make_update(callback_data=edit), context))
    run(ctl.handle_text(make_update(text="81.9"), context))
    updated = store.get_own_day(client_telegram_id=CLIENT_ID, local_date=chosen)["weight_entries"][0]
    assert updated["weight_kg"] == 81.9

    cancel = f"nutrition:weight_cancel:{chosen.isoformat()}:{updated['id']}:{updated['version']}"
    cancel_screen = make_update(callback_data=cancel)
    run(ctl.handle_callback(cancel_screen, context))
    confirm = next(value for value in nutrition_callbacks(cancel_screen.callback_query.message.outbound[-1][1]["reply_markup"])
                   if value.startswith("nutrition:weight_cancel_confirm:"))
    run(ctl.handle_callback(make_update(callback_data=confirm), context))
    assert store.get_own_day(client_telegram_id=CLIENT_ID, local_date=chosen)["weight_entries"] == []


def test_meal_context_wizard_updates_only_after_preview_confirm(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    meal = store.create_meal_draft(
        client_telegram_id=CLIENT_ID, source="manual",
        eaten_at="2026-09-08T08:30:00+03:00", meal_type="завтрак",
        items=[{
            "name": "Омлет", "weight_g": 200, "portion_text": "", "calories": 300,
            "protein_g": 20, "fat_g": 22, "carbs_g": 4, "approximate": False,
        }],
    )
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(
        make_update(callback_data=f"nutrition:meal_context:{meal['id']}"), context
    ))
    nonce = context.user_data["nutrition_mealctx_nonce"]
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{nonce}:type_lunch"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{nonce}:date_yesterday"), context))
    run(ctl.handle_text(make_update(text="13:30"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{nonce}:note_none"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{nonce}:hunger_6"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{nonce}:mood_good"), context))
    before = store.get_own_meal(client_telegram_id=CLIENT_ID, meal_id=meal["id"])
    assert before["meal_type"] == "завтрак"
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_confirm:{nonce}"), context))
    after = store.get_own_meal(client_telegram_id=CLIENT_ID, meal_id=meal["id"])
    assert after["meal_type"] == "обед"
    assert after["hunger_level"] == 6
    assert after["mood"] == "good"
    local_moment = datetime.fromisoformat(after["eaten_at"]).astimezone(ZoneInfo("Europe/Moscow"))
    assert local_moment.date() == ctl._today(CLIENT_ID) - timedelta(days=1)
    assert local_moment.strftime("%H:%M") == "13:30"


def test_old_meal_context_confirm_cannot_write_after_switching_meals(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())

    def meal(name):
        return store.create_meal_draft(
            client_telegram_id=CLIENT_ID, source="manual",
            eaten_at="2026-09-08T08:30:00+03:00", meal_type="завтрак",
            items=[{
                "name": name, "weight_g": 100, "portion_text": "", "calories": 100,
                "protein_g": 5, "fat_g": 2, "carbs_g": 15, "approximate": False,
            }],
        )

    first = meal("Первый")
    second = meal("Второй")
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(
        make_update(callback_data=f"nutrition:meal_context:{first['id']}"), context
    ))
    first_nonce = context.user_data["nutrition_mealctx_nonce"]
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{first_nonce}:type_lunch"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{first_nonce}:date_today"), context))
    run(ctl.handle_text(make_update(text="13:00"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{first_nonce}:note_none"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{first_nonce}:hunger_none"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:mealctx_choice:{first_nonce}:mood_none"), context))
    assert context.user_data["nutrition_state"] == "mealctx_preview"

    run(ctl.handle_callback(
        make_update(callback_data=f"nutrition:meal_context:{second['id']}"), context
    ))
    second_nonce = context.user_data["nutrition_mealctx_nonce"]
    stale = make_update(callback_data=f"nutrition:mealctx_confirm:{first_nonce}")
    run(ctl.handle_callback(stale, context))

    assert second_nonce != first_nonce
    assert context.user_data["nutrition_mealctx_meal_id"] == second["id"]
    assert context.user_data["nutrition_mealctx_nonce"] == second_nonce
    assert store.get_own_meal(client_telegram_id=CLIENT_ID, meal_id=first["id"])["meal_type"] == "завтрак"
    assert store.get_own_meal(client_telegram_id=CLIENT_ID, meal_id=second["id"])["meal_type"] == "завтрак"
    assert "устарел" in stale.callback_query.message.outbound[-1][0]


def test_browser_cabinet_link_is_created_only_for_configured_https_origin(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})
    unavailable = make_update(callback_data="nutrition:cabinet_link")
    run(ctl.handle_callback(unavailable, context))
    assert "не подключен" in unavailable.callback_query.message.outbound[-1][0]

    ctl = controller(store, dashboard_origin="https://dashboard.example")
    opened = make_update(callback_data="nutrition:cabinet_link")
    run(ctl.handle_callback(opened, context))
    markup = opened.callback_query.message.outbound[-1][1]["reply_markup"]
    button = markup.inline_keyboard[0][0]
    assert button.url.startswith("https://dashboard.example/login#token=")
    assert "10 минут" in opened.callback_query.message.outbound[-1][0]


def test_empty_week_formats_none_averages_without_implying_zero_consumption(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    today = ctl._today(CLIENT_ID)
    summary = store.get_own_week(
        client_telegram_id=CLIENT_ID,
        week_start=today - timedelta(days=today.weekday()),
    )
    text = ctl._format_week(summary)
    assert "Среднее по питанию: нет подтвержденных записей" in text
    assert "Среднее по воде: нет записей" in text
    assert "нет записей о воде" in text


def test_client_reminder_wizard_is_opt_in_atomic_and_resumable(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(make_update(callback_data="nutrition:reminders"), context))
    nonce = context.user_data["nutrition_clientrem_nonce"]
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:clientrem_start:{nonce}"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:clientrem_choice:{nonce}:meal_on"), context))
    run(ctl.handle_callback(make_update(callback_data="nutrition:help"), context))
    run(ctl.handle_callback(make_update(callback_data="nutrition:help_resume"), context))
    assert context.user_data["nutrition_state"] == "clientrem_meal_times"
    run(ctl.handle_text(make_update(text="08:00, 13:00, 19:00"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:clientrem_choice:{nonce}:water_interval"), context))
    run(ctl.handle_text(make_update(text="120"), context))
    run(ctl.handle_text(make_update(text="08:00-22:00"), context))
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:clientrem_choice:{nonce}:quiet_off"), context))
    assert store.get_client_reminder_preferences(telegram_id=CLIENT_ID)["enabled"] is False
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:clientrem_confirm:{nonce}"), context))
    prefs = store.get_client_reminder_preferences(telegram_id=CLIENT_ID)
    assert prefs["enabled"] is True
    assert prefs["meal"]["times"] == ["08:00", "13:00", "19:00"]
    assert prefs["water"]["interval_minutes"] == 120


def test_client_can_disable_all_reminders_only_after_preview_confirm(store):
    ctl = controller(store)
    ctl._ensure_user(make_update())
    store.update_client_reminder_preferences(
        telegram_id=CLIENT_ID,
        updates={"enabled": True, "meal": {"enabled": True, "times": ["08:00"]}},
        expected_version=0, idempotency_key="seed-reminders",
    )
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(make_update(callback_data="nutrition:reminders"), context))
    nonce = context.user_data["nutrition_clientrem_nonce"]
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:clientrem_choice:{nonce}:disable"), context))
    assert store.get_client_reminder_preferences(telegram_id=CLIENT_ID)["enabled"] is True
    run(ctl.handle_callback(make_update(callback_data=f"nutrition:clientrem_confirm:{nonce}"), context))
    assert store.get_client_reminder_preferences(telegram_id=CLIENT_ID)["enabled"] is False


def test_trainer_digest_wizard_confirms_without_sending_messages(store):
    ctl = controller(store, trainer_ids={TRAINER_ID})
    ctl._ensure_user(make_update(user_id=TRAINER_ID))
    context = make_context(state={"nutrition_active": True})
    run(ctl.handle_callback(make_update(user_id=TRAINER_ID, callback_data="nutrition:trainer_reminders"), context))
    nonce = context.user_data["nutrition_trainerrem_nonce"]
    run(ctl.handle_callback(make_update(user_id=TRAINER_ID, callback_data=f"nutrition:trainerrem_start:{nonce}"), context))
    run(ctl.handle_text(make_update(user_id=TRAINER_ID, text="20:30"), context))
    run(ctl.handle_text(make_update(user_id=TRAINER_ID, text="2"), context))
    run(ctl.handle_callback(make_update(user_id=TRAINER_ID, callback_data=f"nutrition:trainerrem_choice:{nonce}:compare_on"), context))
    run(ctl.handle_text(make_update(user_id=TRAINER_ID, text="10"), context))
    run(ctl.handle_callback(make_update(user_id=TRAINER_ID, callback_data=f"nutrition:trainerrem_confirm:{nonce}"), context))
    prefs = store.get_trainer_reminder_preferences(trainer_telegram_id=TRAINER_ID)
    assert prefs["enabled"] is True
    assert prefs["daily_digest"]["time"] == "20:30"
    assert prefs["daily_digest"]["inactivity_days"] == 2
    assert prefs["daily_digest"]["over_plan_percent"] == 10
    assert context.bot.file_ids == []
