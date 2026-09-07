import asyncio
import importlib.util
import re
import sys
import types
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
    def __init__(self, text=None, photo=None, message_id=10):
        self.text = text
        self.photo = photo or []
        self.message_id = message_id
        self.outbound = []

    async def reply_text(self, text, **kwargs):
        self.outbound.append((text, kwargs))


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
    callback_data=None,
    callback_id="callback-1",
    message_id=10,
):
    photo = [] if photo_file_id is None else [SimpleNamespace(file_id=photo_file_id)]
    message = FakeMessage(text=text, photo=photo, message_id=message_id)
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
    return SimpleNamespace(user_data=user_data, bot=bot or FakeBot())


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
    assert "передано OpenAI" in photo.message.outbound[-1][0]
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
    submit = make_update(
        user_id=TRAINER_ID,
        text="2033-05-18; 2000; 120; 70; 240; 2200",
    )
    assert run(ctl.handle_text(submit, trainer_context)) is True
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
        SimpleNamespace(PHOTO=Filter(), TEXT=Filter(), COMMAND=Filter()),
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
    assert {"nutrition", "today", "week"} <= registered_commands
    assert dashboard_calls == []
    assert bot.nutrition_dashboard is None
    assert bot.nutrition.dashboard_origin == ""
    trainer_update = make_update(user_id=TRAINER_ID, callback_data="nutrition:trainer")
    run(bot.nutrition._show_trainer_clients(trainer_update))
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
