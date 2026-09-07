import asyncio
import importlib.util
import re
import sys
import types
from datetime import date
from pathlib import Path
from types import SimpleNamespace

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


def controller(store, ai=None, trainer_ids=None):
    return NutritionBotController(
        store=store,
        ai=ai or DisabledAI(),
        trainer_ids=set(trainer_ids or []),
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
    assert "nutrition:add_meal" in nutrition_callbacks(kwargs["reply_markup"])
    assert run(ctl.handle_callback(make_update(callback_data="smm:themes"), context)) is False
    assert run(ctl.handle_text(make_update(text="обычный SMM запрос"), make_context())) is False


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
    assert "6 полей" in manual.message.outbound[-1][0].lower()


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
            "nutrition_pending_source": "photo",
            "nutrition_pending_photo_file_id": "file-1",
        }
    )

    assert run(ctl.handle_text(make_update(text="порция 300 г"), context)) is True
    assert context.user_data["nutrition_pending_description"] == "гречка и курица"
    assert context.user_data["nutrition_pending_source"] == "photo"
    assert context.user_data["nutrition_pending_photo_file_id"] == "file-1"


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
    assert context.user_data["nutrition_state"] == "manual_only"
    assert "структурному формату" in update.message.outbound[-1][0]


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
