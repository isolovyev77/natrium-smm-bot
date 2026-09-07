"""Telegram-сценарии дневника питания.

Контроллер подключается к существующему TelegramSMMBot и обрабатывает только
callback с префиксом ``nutrition:`` и сообщения активной nutrition-сессии.
Все записи сначала создаются как черновик и попадают в статистику лишь после
явного подтверждения пользователя.
"""

from __future__ import annotations

import asyncio
import html
import math
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from src.nutrition_ai import NutritionAI, NutritionAIResponseError, NutritionAIUnavailable
from src.nutrition_plan import NutritionPlanError, build_template, parse_plan
from src.nutrition_reference import FoodReference, FoodReferenceError
from src.nutrition_store import NutritionStore


PHOTO_CONSENT_VERSION = "nutrition-photo-v1"


def parse_trainer_ids(value: str | None = None) -> set[int]:
    """Читает allowlist тренеров, с fallback на существующего администратора."""
    raw = value if value is not None else os.getenv("TRAINER_TELEGRAM_IDS", "")
    if not raw.strip():
        raw = os.getenv("ADMIN_TELEGRAM_ID", "")
    result = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                continue
    return result


class NutritionBotController:
    """Дневник питания для встраивания в существующий Telegram application.

    Public API:
        ``start(update, context)`` открывает nutrition-меню.
        ``handle_callback(update, context) -> bool`` обрабатывает nutrition callback.
        ``handle_text(update, context) -> bool`` обрабатывает активный текстовый шаг.
        ``handle_photo(update, context) -> bool`` обрабатывает фото активной сессии.
        ``is_active(context)`` сообщает, принадлежит ли текущий текст nutrition flow.
    """

    _NORMS_FIELDS = (
        ("calories", "Калории", "ккал/сутки"),
        ("protein_g", "Белки", "г/сутки"),
        ("fat_g", "Жиры", "г/сутки"),
        ("carbs_g", "Углеводы", "г/сутки"),
        ("water_ml", "Вода", "мл/сутки"),
    )

    def __init__(
        self,
        *,
        store: NutritionStore,
        ai: NutritionAI | None = None,
        trainer_ids: set[int] | None = None,
        dashboard_origin: str | None = None,
        reference: FoodReference | None = None,
    ):
        self.store = store
        self.ai = ai or NutritionAI()
        self.trainer_ids = trainer_ids if trainer_ids is not None else parse_trainer_ids()
        try:
            self.reference = reference if reference is not None else FoodReference()
            self.reference_error = None
        except FoodReferenceError as exc:
            self.reference = None
            self.reference_error = exc
        # Веб-кнопка появляется только после явного успешного bootstrap панели.
        # Значение из окружения проверяет интеграционный слой TelegramSMMBot.
        self.dashboard_origin = (dashboard_origin or "").rstrip("/")

    @staticmethod
    def is_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
        return bool(context.user_data.get("nutrition_active"))

    def is_trainer(self, telegram_id: int) -> bool:
        return telegram_id in self.trainer_ids

    def reset(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._clear(context)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._private_only(update, context):
            return
        self._ensure_user(update)
        self._clear(context)
        context.user_data["nutrition_active"] = True
        await self._send_menu(update, context)

    async def show_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показывает личную дневную сводку для команды /today."""
        if not await self._private_only(update, context):
            return
        self._ensure_user(update)
        self._clear(context)
        context.user_data["nutrition_active"] = True
        summary = self.store.get_own_day(
            client_telegram_id=update.effective_user.id,
            local_date=self._today(update.effective_user.id),
        )
        await self._reply(
            update,
            self._format_day(summary),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🥗 В дневник", callback_data="nutrition:menu")]]
            ),
        )

    async def show_week(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показывает личную недельную сводку для команды /week."""
        if not await self._private_only(update, context):
            return
        self._ensure_user(update)
        self._clear(context)
        context.user_data["nutrition_active"] = True
        today = self._today(update.effective_user.id)
        summary = self.store.get_own_week(
            client_telegram_id=update.effective_user.id,
            week_start=today - timedelta(days=today.weekday()),
        )
        await self._reply(
            update,
            self._format_week(summary),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🥗 В дневник", callback_data="nutrition:menu")]]
            ),
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        query = update.callback_query
        if query is None or not (query.data or "").startswith("nutrition:"):
            return False
        if not await self._private_only(update, context):
            return True
        self._ensure_user(update)
        context.user_data["nutrition_active"] = True
        data = query.data
        try:
            if data == "nutrition:menu":
                self._clear(context)
                context.user_data["nutrition_active"] = True
                await self._send_menu(update, context, edit=True)
            elif data == "nutrition:add_meal":
                self._clear_reference_context(context)
                keyboard = [
                    [InlineKeyboardButton("📚 Рассчитать по справочнику", callback_data="nutrition:meal_reference")],
                    [InlineKeyboardButton("📸 Фото еды", callback_data="nutrition:meal_photo")],
                    [InlineKeyboardButton("✍️ Описание через OpenAI", callback_data="nutrition:meal_text_ai")],
                    [InlineKeyboardButton("⌨️ Ввести КБЖУ самому", callback_data="nutrition:meal_manual")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:menu")],
                ]
                await query.edit_message_text(
                    "🍽️ <b>Новый прием пищи</b>\n\nВыберите способ ввода:",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            elif data == "nutrition:meal_reference":
                if self.reference is None:
                    await query.edit_message_text(
                        "Справочник временно недоступен. Можно ввести КБЖУ вручную.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("⌨️ Ручной ввод", callback_data="nutrition:meal_manual")],
                            [InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:add_meal")],
                        ]),
                    )
                else:
                    self._clear_reference_context(context)
                    context.user_data["nutrition_state"] = "reference_search"
                    context.user_data["nutrition_reference_mode"] = "build"
                    context.user_data["nutrition_reference_items"] = []
                    await query.edit_message_text(
                        "Введите продукт и способ приготовления, например «рис вареный» или "
                        "«куриная грудка запеченная». Жирность и состояние продукта выбираются "
                        "только по найденной записи.",
                        reply_markup=self._cancel_keyboard("nutrition:add_meal"),
                    )
            elif data == "nutrition:meal_photo":
                context.user_data["nutrition_state"] = "wait_photo"
                await query.edit_message_text(
                    "📸 Пришлите одно фото еды. Сначала будет создан черновик, "
                    "который вы сможете исправить, подтвердить или отменить.",
                    reply_markup=self._cancel_keyboard("nutrition:add_meal"),
                )
            elif data == "nutrition:meal_manual":
                context.user_data["nutrition_state"] = "manual_only"
                await query.edit_message_text(
                    "Ручной ввод без AI.\n\n" + self._manual_prompt(),
                    reply_markup=self._cancel_keyboard("nutrition:add_meal"),
                )
            elif data == "nutrition:meal_text_ai":
                context.user_data["nutrition_state"] = "wait_manual"
                await query.edit_message_text(
                    "Ваше описание будет передано OpenAI для приблизительной оценки. "
                    "После этого вы проверите черновик. Опишите еду и размер порции:",
                    reply_markup=self._cancel_keyboard("nutrition:add_meal"),
                )
            elif data.startswith("nutrition:ref_pick:"):
                await self._pick_reference_candidate(update, context, data.rsplit(":", 1)[1])
            elif data == "nutrition:ref_more":
                if self.reference is None:
                    raise ValueError("Справочник временно недоступен")
                context.user_data["nutrition_state"] = "reference_search"
                await query.edit_message_text(
                    "Введите следующий продукт и способ приготовления:",
                    reply_markup=self._cancel_keyboard("nutrition:ref_review"),
                )
            elif data == "nutrition:ref_review":
                await self._show_reference_builder(update, context)
            elif data == "nutrition:ref_back_candidates":
                if self.reference is None:
                    raise ValueError("Справочник временно недоступен")
                candidates = [
                    self.reference.get(fdc_id)
                    for fdc_id in context.user_data.get("nutrition_reference_candidates", [])
                ]
                await self._show_reference_candidates(
                    update, context, candidates,
                    query=context.user_data.get("nutrition_reference_query", "продукт"),
                )
            elif data == "nutrition:ref_finish":
                items = context.user_data.get("nutrition_reference_items") or []
                if not items:
                    raise ValueError("Сначала добавьте хотя бы один продукт")
                await self._create_draft(update, context, items, source="manual")
                self._clear_reference_context(context, keep_state=True)
            elif data.startswith("nutrition:refine:"):
                meal_id = int(data.rsplit(":", 1)[1])
                await self._show_refine_items(update, context, meal_id)
            elif data.startswith("nutrition:draft:"):
                meal_id = int(data.rsplit(":", 1)[1])
                meal = self.store.get_owned_draft(
                    client_telegram_id=update.effective_user.id, meal_id=meal_id
                )
                await self._show_draft(update, context, meal)
            elif data.startswith("nutrition:refine_item:"):
                item_id = int(data.rsplit(":", 1)[1])
                await self._search_for_draft_item(update, context, item_id)
            elif data.startswith("nutrition:ref_weight:"):
                item_id = int(data.rsplit(":", 1)[1])
                meal_id = int(context.user_data.get("nutrition_reference_meal_id", 0))
                meal = self.store.get_owned_draft(
                    client_telegram_id=update.effective_user.id, meal_id=meal_id
                )
                if not any(item["id"] == item_id and item["calculation_method"] == "reference" for item in meal["items"]):
                    raise PermissionError("Позиция справочника не найдена в этом черновике")
                context.user_data["nutrition_reference_item_id"] = item_id
                context.user_data["nutrition_state"] = "reference_reweight"
                await query.edit_message_text(
                    "Введите новую массу продукта в граммах:",
                    reply_markup=self._cancel_keyboard(f"nutrition:refine:{meal_id}"),
                )
            elif data == "nutrition:consent_yes":
                self.store.set_photo_consent(
                    telegram_id=update.effective_user.id,
                    version=PHOTO_CONSENT_VERSION,
                )
                file_id = context.user_data.pop("nutrition_pending_photo", None)
                if not file_id:
                    raise ValueError("Фото для анализа не найдено, пришлите его еще раз")
                await query.edit_message_text("🔎 Анализирую фото и готовлю черновик...")
                await self._analyze_photo(update, context, file_id)
            elif data == "nutrition:consent_no":
                context.user_data.pop("nutrition_pending_photo", None)
                context.user_data["nutrition_state"] = "manual_only"
                await query.edit_message_text(
                    "Фото не отправлено OpenAI. Введите значения вручную.\n\n" +
                    self._manual_prompt(),
                    reply_markup=self._cancel_keyboard("nutrition:add_meal"),
                )
            elif data == "nutrition:consent_revoke":
                self.store.revoke_photo_consent(telegram_id=update.effective_user.id)
                context.user_data.pop("nutrition_pending_photo", None)
                context.user_data["nutrition_state"] = "manual_only"
                await query.edit_message_text(
                    "Согласие отозвано. Новые фото не будут передаваться OpenAI без "
                    "нового согласия. Ручной ввод остается доступен."
                )
            elif data.startswith("nutrition:confirm:"):
                meal_id = int(data.rsplit(":", 1)[1])
                meal = self.store.confirm_meal(
                    client_telegram_id=update.effective_user.id,
                    meal_id=meal_id,
                    idempotency_key=f"meal:{update.effective_user.id}:{meal_id}:confirm",
                )
                context.user_data.pop("nutrition_state", None)
                await query.edit_message_text(
                    f"✅ Прием пищи #{meal['id']} сохранен. Данные учтены в статистике.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🥗 В дневник", callback_data="nutrition:menu")]]
                    ),
                )
            elif data.startswith("nutrition:edit:"):
                meal_id = int(data.rsplit(":", 1)[1])
                context.user_data["nutrition_state"] = "edit_draft"
                context.user_data["nutrition_edit_meal_id"] = meal_id
                await query.edit_message_text(
                    "Пришлите исправленный список, каждое блюдо с новой строки:\n"
                    "Название; масса в г или описание порции; ккал; белки; жиры; углеводы",
                    reply_markup=self._cancel_keyboard(f"nutrition:draft:{meal_id}"),
                )
            elif data.startswith("nutrition:cancel:"):
                meal_id = int(data.rsplit(":", 1)[1])
                self.store.cancel_meal(client_telegram_id=update.effective_user.id, meal_id=meal_id)
                context.user_data.pop("nutrition_state", None)
                await query.edit_message_text(
                    "Отменено. Черновик не попал в статистику.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🥗 В дневник", callback_data="nutrition:menu")]]
                    ),
                )
            elif data == "nutrition:water":
                keyboard = [
                    [
                        InlineKeyboardButton("250 мл", callback_data="nutrition:water:250"),
                        InlineKeyboardButton("500 мл", callback_data="nutrition:water:500"),
                    ],
                    [InlineKeyboardButton("Другой объем", callback_data="nutrition:water_custom")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:menu")],
                ]
                await query.edit_message_text("💧 Сколько воды добавить?", reply_markup=InlineKeyboardMarkup(keyboard))
            elif data.startswith("nutrition:water:"):
                amount = int(data.rsplit(":", 1)[1])
                self.store.add_water(
                    client_telegram_id=update.effective_user.id,
                    amount_ml=amount,
                    logged_at=datetime.now(timezone.utc),
                    idempotency_key=f"water:{query.id}",
                )
                await query.edit_message_text(
                    f"💧 Добавлено {amount} мл.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🥗 В дневник", callback_data="nutrition:menu")]]
                    ),
                )
            elif data == "nutrition:water_custom":
                context.user_data["nutrition_state"] = "wait_water"
                await query.edit_message_text(
                    "Введите объем воды целым числом в миллилитрах:",
                    reply_markup=self._cancel_keyboard("nutrition:water"),
                )
            elif data == "nutrition:today":
                summary = self.store.get_own_day(
                    client_telegram_id=update.effective_user.id,
                    local_date=self._today(update.effective_user.id),
                )
                await query.edit_message_text(
                    self._format_day(summary),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:menu")]]
                    ),
                )
            elif data == "nutrition:week":
                today = self._today(update.effective_user.id)
                week_start = today - timedelta(days=today.weekday())
                summary = self.store.get_own_week(
                    client_telegram_id=update.effective_user.id,
                    week_start=week_start,
                )
                await query.edit_message_text(
                    self._format_week(summary),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:menu")]]
                    ),
                )
            elif data == "nutrition:link":
                context.user_data["nutrition_state"] = "wait_link_code"
                await query.edit_message_text(
                    "Введите код, который прислал ваш тренер:",
                    reply_markup=self._cancel_keyboard("nutrition:menu"),
                )
            elif data == "nutrition:unlink":
                self.store.unlink_trainer(client_telegram_id=update.effective_user.id)
                await query.edit_message_text(
                    "Связь с тренером отключена.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🥗 В дневник", callback_data="nutrition:menu")]]
                    ),
                )
            elif data == "nutrition:timezone":
                context.user_data["nutrition_state"] = "wait_timezone"
                await query.edit_message_text(
                    "Введите часовой пояс в формате IANA, например Europe/Moscow или Asia/Tokyo:",
                    reply_markup=self._cancel_keyboard("nutrition:menu"),
                )
            elif data == "nutrition:trainer":
                self._require_trainer(update.effective_user.id)
                self._clear_pending_trainer_action(context)
                await self._show_trainer_clients(update)
            elif data == "nutrition:invite":
                self._require_trainer(update.effective_user.id)
                invite = self.store.get_or_create_trainer_code(
                    trainer_telegram_id=update.effective_user.id
                )
                await query.edit_message_text(
                    "Постоянный код для подключения клиентов:\n\n"
                    f"<code>{html.escape(invite['code'])}</code>\n\n"
                    "Любой, кому вы передадите код, сможет подключиться к вам. "
                    "Замените его, чтобы закрыть старый код; уже подключенные клиенты останутся.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("🔄 Заменить код", callback_data="nutrition:replace_invite")],
                            [InlineKeyboardButton("⬅️ Кабинет", callback_data="nutrition:trainer")],
                        ]
                    ),
                )
            elif data == "nutrition:replace_invite":
                self._require_trainer(update.effective_user.id)
                await query.edit_message_text(
                    "Старый код сразу перестанет принимать новых клиентов. "
                    "Уже подключенные клиенты останутся. Создать новый код?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Да, заменить", callback_data="nutrition:replace_invite_confirm")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:invite")],
                        [InlineKeyboardButton("Отмена", callback_data="nutrition:trainer")],
                    ]),
                )
            elif data == "nutrition:replace_invite_confirm":
                self._require_trainer(update.effective_user.id)
                invite = self.store.replace_trainer_code(
                    trainer_telegram_id=update.effective_user.id
                )
                await query.edit_message_text(
                    "Новый постоянный код:\n\n"
                    f"<code>{html.escape(invite['code'])}</code>\n\n"
                    "Старый код закрыт для новых подключений.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Кабинет", callback_data="nutrition:trainer")]
                    ]),
                )
            elif data.startswith("nutrition:trainer_day:"):
                self._require_trainer(update.effective_user.id)
                client_id = int(data.rsplit(":", 1)[1])
                self._clear_pending_trainer_action(context)
                summary = self.store.get_client_day(
                    update.effective_user.id,
                    client_id,
                    self._today_for_client(update.effective_user.id, client_id),
                )
                await query.edit_message_text(
                    self._format_day(summary, trainer=True),
                    parse_mode="HTML",
                    reply_markup=self._trainer_client_keyboard(client_id),
                )
            elif data.startswith("nutrition:trainer_week:"):
                self._require_trainer(update.effective_user.id)
                client_id = int(data.rsplit(":", 1)[1])
                today = self._today_for_client(update.effective_user.id, client_id)
                summary = self.store.get_client_week(
                    update.effective_user.id, client_id, today - timedelta(days=today.weekday())
                )
                await query.edit_message_text(
                    self._format_week(summary),
                    parse_mode="HTML",
                    reply_markup=self._trainer_client_keyboard(client_id),
                )
            elif data.startswith("nutrition:trainer_norms:"):
                self._require_trainer(update.effective_user.id)
                client_id = int(data.rsplit(":", 1)[1])
                await self._start_norms_wizard(update, context, client_id)
            elif data.startswith("nutrition:norms_date:"):
                self._require_trainer(update.effective_user.id)
                _, _, client_raw, nonce, choice = data.split(":", 4)
                client_id = int(client_raw)
                self._require_flow_session(context, "norms", client_id, nonce)
                self.store.get_client_day(
                    update.effective_user.id, client_id,
                    self._today_for_client(update.effective_user.id, client_id),
                )
                if choice == "other":
                    context.user_data["nutrition_state"] = "norms_date_other"
                    await query.edit_message_text(
                        "Введите дату начала действия в формате ГГГГ-ММ-ДД:",
                        reply_markup=self._cancel_keyboard(
                            f"nutrition:norms_date_back:{client_id}:{nonce}"
                        ),
                    )
                else:
                    if choice not in {"today", "tomorrow"}:
                        raise ValueError("Неизвестный вариант даты")
                    today = self._today_for_client(update.effective_user.id, client_id)
                    chosen = today if choice == "today" else today + timedelta(days=1)
                    context.user_data["nutrition_norms_draft"]["effective_from"] = chosen.isoformat()
                    await self._show_norms_step(update, context, 0)
            elif data.startswith("nutrition:norms_choice:"):
                self._require_trainer(update.effective_user.id)
                _, _, client_raw, nonce, choice = data.split(":", 4)
                client_id = int(client_raw)
                self._require_flow_session(
                    context, "norms", client_id, nonce, expected_state="norms_value"
                )
                await self._apply_norms_choice(update, context, choice)
            elif data.startswith("nutrition:norms_date_back:"):
                self._require_trainer(update.effective_user.id)
                _, _, client_raw, nonce = data.split(":", 3)
                client_id = int(client_raw)
                self._require_flow_session(context, "norms", client_id, nonce)
                await self._show_norms_date(update, context)
            elif data.startswith("nutrition:norms_back:"):
                self._require_trainer(update.effective_user.id)
                _, _, client_raw, nonce = data.split(":", 3)
                client_id = int(client_raw)
                self._require_flow_session(context, "norms", client_id, nonce)
                step = int(context.user_data.get("nutrition_norms_step", 0))
                if step <= 0:
                    await self._show_norms_date(update, context)
                else:
                    await self._show_norms_step(update, context, step - 1)
            elif data.startswith("nutrition:norms_confirm:"):
                self._require_trainer(update.effective_user.id)
                _, _, client_raw, nonce = data.split(":", 3)
                client_id = int(client_raw)
                self._require_flow_session(
                    context, "norms", client_id, nonce, expected_state="norms_preview"
                )
                draft = dict(context.user_data.get("nutrition_norms_draft") or {})
                result = self.store.set_norms(
                    update.effective_user.id,
                    client_id,
                    {key: draft.get(key) for key, _, _ in self._NORMS_FIELDS},
                    draft.get("effective_from", ""),
                )
                self._clear_pending_trainer_action(context)
                await query.edit_message_text(
                    f"✅ Дневные нормы сохранены с {result['effective_from']}.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ К клиенту", callback_data=f"nutrition:trainer_day:{client_id}")]
                    ]),
                )
            elif data.startswith("nutrition:trainer_plan_upload:"):
                self._require_trainer(update.effective_user.id)
                client_id = int(data.rsplit(":", 1)[1])
                self.store.get_client_day(
                    update.effective_user.id, client_id,
                    self._today_for_client(update.effective_user.id, client_id),
                )
                self._clear_pending_trainer_action(context)
                context.user_data["nutrition_state"] = "trainer_plan_upload"
                context.user_data["nutrition_trainer_client_id"] = client_id
                context.user_data["nutrition_plan_nonce"] = self._new_flow_nonce()
                await query.edit_message_text(
                    "Пришлите файл .xlsx до 2 МБ. Каждая строка задает дневные нормы "
                    "с указанной даты до следующей строки плана.",
                    reply_markup=self._cancel_keyboard(f"nutrition:trainer_day:{client_id}"),
                )
            elif data.startswith("nutrition:trainer_plan_template:"):
                self._require_trainer(update.effective_user.id)
                client_id = int(data.rsplit(":", 1)[1])
                self.store.get_client_day(
                    update.effective_user.id, client_id,
                    self._today_for_client(update.effective_user.id, client_id),
                )
                template = build_template()
                await query.message.reply_document(
                    document=template,
                    filename="natrium-norms-plan.xlsx",
                    caption="Шаблон плана дневных норм. Заполните лист «План».",
                )
            elif data.startswith("nutrition:trainer_plan_confirm:"):
                self._require_trainer(update.effective_user.id)
                _, _, client_raw, nonce = data.split(":", 3)
                client_id = int(client_raw)
                self._require_flow_session(
                    context, "plan", client_id, nonce, expected_state="trainer_plan_preview"
                )
                rows = context.user_data.get("nutrition_pending_plan") or []
                applied = self.store.set_norms_bulk(update.effective_user.id, client_id, rows)
                self._clear_pending_trainer_action(context)
                await query.edit_message_text(
                    f"✅ План применен: {len(applied)} строк. Каждая норма действует "
                    "до даты следующей строки.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ К клиенту", callback_data=f"nutrition:trainer_day:{client_id}")]
                    ]),
                )
            elif data.startswith("nutrition:trainer_edit:"):
                self._require_trainer(update.effective_user.id)
                client_id = int(data.rsplit(":", 1)[1])
                await self._show_trainer_meal_picker(update, context, client_id, action="edit")
            elif data.startswith("nutrition:trainer_comment:"):
                self._require_trainer(update.effective_user.id)
                client_id = int(data.rsplit(":", 1)[1])
                await self._show_trainer_meal_picker(update, context, client_id, action="comment")
            elif data.startswith("nutrition:tcm:"):
                meal_id = int(data.rsplit(":", 1)[1])
                meal = self.store.get_client_meal(update.effective_user.id, meal_id)
                context.user_data["nutrition_state"] = "trainer_comment_text"
                context.user_data["nutrition_trainer_meal_id"] = meal_id
                context.user_data["nutrition_trainer_client_id"] = meal["client_user_id"]
                await query.edit_message_text(
                    self._meal_context(meal) + "\n\nНапишите комментарий обычным текстом:",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ К выбору записи", callback_data=f"nutrition:trainer_comment:{meal['client_user_id']}")],
                        [InlineKeyboardButton("Отмена", callback_data=f"nutrition:trainer_day:{meal['client_user_id']}")],
                    ]),
                )
            elif data.startswith("nutrition:tem:"):
                meal_id = int(data.rsplit(":", 1)[1])
                meal = self.store.get_client_meal(update.effective_user.id, meal_id)
                context.user_data["nutrition_state"] = "trainer_edit_items"
                context.user_data["nutrition_trainer_meal_id"] = meal_id
                context.user_data["nutrition_trainer_client_id"] = meal["client_user_id"]
                await query.edit_message_text(
                    self._meal_context(meal) + "\n\nПришлите исправленные блюда по одному на строку:\n"
                    "Название; масса в г или описание порции; ккал; белки; жиры; углеводы",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ К выбору записи", callback_data=f"nutrition:trainer_edit:{meal['client_user_id']}")],
                        [InlineKeyboardButton("Отмена", callback_data=f"nutrition:trainer_day:{meal['client_user_id']}")],
                    ]),
                )
            elif data.startswith("nutrition:trainer_meal:"):
                meal_id = int(data.rsplit(":", 1)[1])
                meal = self.store.get_client_meal(update.effective_user.id, meal_id)
                await query.edit_message_text(
                    self._meal_context(meal, include_comments=True),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 Комментировать", callback_data=f"nutrition:tcm:{meal_id}")],
                        [InlineKeyboardButton("⬅️ К клиенту", callback_data=f"nutrition:trainer_day:{meal['client_user_id']}")],
                    ]),
                )
            elif data == "nutrition:leave":
                self._clear(context)
                await query.edit_message_text("Режим дневника закрыт. Используйте кнопки основного меню.")
            else:
                return False
        except (ValueError, PermissionError, FoodReferenceError, NutritionPlanError) as exc:
            await query.message.reply_text(f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        return True

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if not self.is_active(context) or context.user_data.get("nutrition_state") != "wait_photo":
            return False
        if not await self._private_only(update, context):
            return True
        self._ensure_user(update)
        if not update.message or not update.message.photo:
            return False
        file_id = update.message.photo[-1].file_id
        if not self.store.has_photo_consent(
            telegram_id=update.effective_user.id,
            version=PHOTO_CONSENT_VERSION,
        ):
            context.user_data["nutrition_pending_photo"] = file_id
            keyboard = [
                [InlineKeyboardButton("Согласен, анализировать", callback_data="nutrition:consent_yes")],
                [InlineKeyboardButton("Без AI, введу текст", callback_data="nutrition:consent_no")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:add_meal")],
                [InlineKeyboardButton("Отмена", callback_data="nutrition:menu")],
            ]
            await update.message.reply_text(
                "Для разбора фото оно будет передано OpenAI. Анализ создает "
                "приблизительный черновик, который не сохраняется без вашего подтверждения. "
                "Согласие сохранится для следующих фото, его можно отозвать в меню. "
                "Если вы не согласны, используйте ручной ввод. Передавать фото OpenAI?",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return True
        await update.message.reply_text("🔎 Анализирую фото и готовлю черновик...")
        await self._analyze_photo(update, context, file_id)
        return True

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if context.user_data.get("nutrition_state") != "trainer_plan_upload":
            return False
        if not await self._private_only(update, context):
            return True
        self._require_trainer(update.effective_user.id)
        client_id = int(context.user_data.get("nutrition_trainer_client_id", 0))
        self.store.get_client_day(
            update.effective_user.id, client_id,
            self._today_for_client(update.effective_user.id, client_id),
        )
        document = update.message.document if update.message else None
        if document is None:
            return False
        if not (document.file_name or "").lower().endswith(".xlsx"):
            await update.message.reply_text(
                "Нужен файл .xlsx из шаблона.",
                reply_markup=self._cancel_keyboard(f"nutrition:trainer_day:{client_id}"),
            )
            return True
        if document.file_size and document.file_size > 2 * 1024 * 1024:
            await update.message.reply_text(
                "Файл больше 2 МБ. Уменьшите план и повторите загрузку.",
                reply_markup=self._cancel_keyboard(f"nutrition:trainer_day:{client_id}"),
            )
            return True
        try:
            telegram_file = await context.bot.get_file(document.file_id)
            payload = bytes(await telegram_file.download_as_bytearray())
            rows = parse_plan(payload)
        except NutritionPlanError as exc:
            await update.message.reply_text(
                f"⚠️ {html.escape(str(exc))}", parse_mode="HTML",
                reply_markup=self._cancel_keyboard(f"nutrition:trainer_day:{client_id}"),
            )
            return True
        except Exception:
            await update.message.reply_text(
                "Не удалось безопасно получить файл. Повторите загрузку .xlsx.",
                reply_markup=self._cancel_keyboard(f"nutrition:trainer_day:{client_id}"),
            )
            return True
        pending = [
            {
                "effective_from": row.effective_from.isoformat(),
                "calories": float(row.calories),
                "protein_g": float(row.protein_g),
                "fat_g": float(row.fat_g),
                "carbs_g": float(row.carbs_g),
                "water_ml": row.water_ml,
            }
            for row in rows
        ]
        context.user_data["nutrition_pending_plan"] = pending
        context.user_data["nutrition_state"] = "trainer_plan_preview"
        nonce = context.user_data.get("nutrition_plan_nonce", "")
        lines = [f"📎 Проверено строк: {len(pending)}."]
        for row in pending[:10]:
            water = "не задана" if row["water_ml"] is None else f"{row['water_ml']} мл/сутки"
            lines.append(
                f"• {row['effective_from']}: {row['calories']:g} ккал/сутки, "
                f"Б {row['protein_g']:g}, Ж {row['fat_g']:g}, У {row['carbs_g']:g} г/сутки, "
                f"вода {water}"
            )
        if len(pending) > 10:
            lines.append(f"…и еще {len(pending) - 10} строк.")
        lines.append("\nИзменятся только даты из файла. Другие даты норм не удаляются.")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "✅ Применить план",
                    callback_data=f"nutrition:trainer_plan_confirm:{client_id}:{nonce}",
                )],
                [InlineKeyboardButton("⬅️ Загрузить другой файл", callback_data=f"nutrition:trainer_plan_upload:{client_id}")],
                [InlineKeyboardButton("Отмена", callback_data=f"nutrition:trainer_day:{client_id}")],
            ]),
        )
        return True

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if not self.is_active(context) or not update.message or not update.message.text:
            return False
        if not await self._private_only(update, context):
            return True
        self._ensure_user(update)
        state = context.user_data.get("nutrition_state")
        text = update.message.text.strip()
        try:
            if state == "reference_search":
                if self.reference is None:
                    raise ValueError("Справочник временно недоступен")
                context.user_data.pop("nutrition_reference_candidates", None)
                candidates = self.reference.search(text, limit=5)
                await self._show_reference_candidates(update, context, candidates, query=text)
            elif state == "reference_grams":
                if self.reference is None:
                    raise ValueError("Справочник временно недоступен")
                fdc_id = context.user_data.get("nutrition_reference_fdc_id")
                calculated = self.reference.calculate(fdc_id, text.replace(",", "."))
                item = self._reference_item(calculated)
                if context.user_data.get("nutrition_reference_mode") == "refine":
                    meal_id = int(context.user_data.get("nutrition_reference_meal_id", 0))
                    item_id = int(context.user_data.get("nutrition_reference_item_id", 0))
                    meal = self.store.replace_draft_item_with_reference(
                        client_telegram_id=update.effective_user.id,
                        meal_id=meal_id,
                        item_id=item_id,
                        item=item,
                    )
                    self._clear_reference_context(context, keep_state=True)
                    await self._show_draft(update, context, meal)
                else:
                    items = context.user_data.setdefault("nutrition_reference_items", [])
                    items.append(item)
                    context.user_data.pop("nutrition_reference_fdc_id", None)
                    await self._show_reference_builder(update, context)
            elif state == "reference_reweight":
                meal_id = int(context.user_data.get("nutrition_reference_meal_id", 0))
                item_id = int(context.user_data.get("nutrition_reference_item_id", 0))
                meal = self.store.update_reference_item_weight(
                    client_telegram_id=update.effective_user.id,
                    meal_id=meal_id,
                    item_id=item_id,
                    grams=text.replace(",", "."),
                )
                self._clear_reference_context(context, keep_state=True)
                await self._show_draft(update, context, meal)
            elif state == "norms_date_other":
                try:
                    chosen = date.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError("Введите дату в формате ГГГГ-ММ-ДД") from exc
                context.user_data["nutrition_norms_draft"]["effective_from"] = chosen.isoformat()
                await self._show_norms_step(update, context, 0)
            elif state == "norms_value":
                step = int(context.user_data.get("nutrition_norms_step", 0))
                key, label, unit = self._NORMS_FIELDS[step]
                try:
                    value = float(text.replace(",", "."))
                except ValueError as exc:
                    raise ValueError(f"Введите {label.lower()} числом, единица: {unit}") from exc
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{label} должны быть конечным неотрицательным числом")
                if key == "water_ml":
                    if not value.is_integer():
                        raise ValueError("Воду укажите целым числом миллилитров в сутки")
                    value = int(value)
                context.user_data["nutrition_norms_draft"][key] = value
                await self._show_norms_step(update, context, step + 1)
            elif state == "wait_manual":
                await self._analyze_or_parse_text(update, context, text, source="manual")
            elif state == "manual_only":
                items = self.parse_structured_items(text)
                await self._create_draft(update, context, items, source="manual")
            elif state == "clarify_ai":
                prior = context.user_data.get("nutrition_pending_description", "")
                source = context.user_data.get("nutrition_pending_source", "manual")
                completed = await self._analyze_or_parse_text(
                    update,
                    context,
                    f"{prior}\nУточнение пользователя: {text}",
                    source=source,
                )
                if completed:
                    context.user_data.pop("nutrition_pending_description", None)
                    context.user_data.pop("nutrition_pending_source", None)
                    context.user_data.pop("nutrition_pending_photo_file_id", None)
            elif state == "edit_draft":
                meal_id = int(context.user_data.get("nutrition_edit_meal_id"))
                items = self.parse_structured_items(text)
                meal = self.store.replace_draft_items(
                    client_telegram_id=update.effective_user.id,
                    meal_id=meal_id,
                    items=items,
                )
                await self._show_draft(update, context, meal)
            elif state == "wait_water":
                amount = int(text)
                row = self.store.add_water(
                    client_telegram_id=update.effective_user.id,
                    amount_ml=amount,
                    logged_at=datetime.now(timezone.utc),
                    idempotency_key=f"water:message:{update.message.message_id}",
                )
                context.user_data.pop("nutrition_state", None)
                await update.message.reply_text(f"💧 Добавлено {row['amount_ml']} мл.")
                await self._send_menu(update, context)
            elif state == "wait_link_code":
                link = self.store.link_client_by_code(
                    client_telegram_id=update.effective_user.id,
                    code=text,
                )
                context.user_data.pop("nutrition_state", None)
                await update.message.reply_text(
                    f"✅ Вы привязаны к тренеру {html.escape(link['trainer_display_name'])}.",
                    parse_mode="HTML",
                )
                await self._send_menu(update, context)
            elif state == "wait_timezone":
                user = self.store.set_timezone(
                    telegram_id=update.effective_user.id,
                    timezone_name=text,
                )
                context.user_data.pop("nutrition_state", None)
                await update.message.reply_text(f"✅ Часовой пояс: {user['timezone']}")
                await self._send_menu(update, context)
            elif state == "trainer_norms":
                self._require_trainer(update.effective_user.id)
                client_id = int(context.user_data["nutrition_trainer_client_id"])
                effective_from, norms = self.parse_norms(text)
                result = self.store.set_norms(
                    update.effective_user.id, client_id, norms, effective_from
                )
                context.user_data.pop("nutrition_state", None)
                await update.message.reply_text(
                    f"✅ Нормы действуют с {result['effective_from']}."
                )
            elif state == "trainer_edit_items":
                self._require_trainer(update.effective_user.id)
                meal_id = int(context.user_data.get("nutrition_trainer_meal_id", 0))
                items = self.parse_structured_items(text)
                meal = self.store.update_meal_as_trainer(
                    update.effective_user.id, meal_id, {"items": items}
                )
                context.user_data.pop("nutrition_state", None)
                await update.message.reply_text(
                    "✅ Запись исправлена.\n\n" + self._meal_context(meal),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ К записи", callback_data=f"nutrition:trainer_meal:{meal_id}")],
                        [InlineKeyboardButton("👤 К клиенту", callback_data=f"nutrition:trainer_day:{meal['client_user_id']}")],
                    ]),
                )
            elif state == "trainer_comment_text":
                self._require_trainer(update.effective_user.id)
                meal_id = int(context.user_data.get("nutrition_trainer_meal_id", 0))
                meal = self.store.get_client_meal(update.effective_user.id, meal_id)
                result = self.store.add_comment(
                    update.effective_user.id, meal_id, text
                )
                context.user_data.pop("nutrition_state", None)
                await update.message.reply_text(
                    "✅ Комментарий добавлен.\n\n" + self._meal_context(meal),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ К записи", callback_data=f"nutrition:trainer_meal:{meal_id}")],
                        [InlineKeyboardButton("👤 К клиенту", callback_data=f"nutrition:trainer_day:{meal['client_user_id']}")],
                    ]),
                )
            else:
                await self._send_menu(update, context)
            return True
        except (ValueError, PermissionError, NutritionAIResponseError) as exc:
            await update.message.reply_text(f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
            return True
        except FoodReferenceError as exc:
            await update.message.reply_text(
                f"⚠️ {html.escape(str(exc))}", parse_mode="HTML",
                reply_markup=self._cancel_keyboard("nutrition:menu"),
            )
            return True

    async def _analyze_photo(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        file_id: str,
    ) -> None:
        if not self.ai.available:
            context.user_data["nutrition_state"] = "manual_only"
            await self._reply(
                update,
                "AI-анализ фото пока не настроен. Фото не сохранено локально. "
                "Опишите еду текстом или используйте формат:\n" + self._manual_prompt(),
            )
            return
        try:
            telegram_file = await context.bot.get_file(file_id)
            image = bytes(await telegram_file.download_as_bytearray())
            result = await asyncio.wait_for(
                asyncio.to_thread(self.ai.analyze_photo, image, "image/jpeg"),
                timeout=self.ai.timeout_seconds + 3,
            )
        except Exception:
            context.user_data["nutrition_state"] = "manual_only"
            await self._reply(
                update,
                "Не удалось безопасно получить или разобрать фото. Фото не учтено. "
                "Введите КБЖУ вручную по структурному формату.",
            )
            return
        await self._accept_ai_result(update, context, result, source="photo", photo_file_id=file_id)

    async def _analyze_or_parse_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        *,
        source: str,
    ) -> bool:
        if ";" in text:
            items = self.parse_structured_items(text)
            await self._create_draft(update, context, items, source=source)
            return True
        if not self.ai.available:
            raise ValueError(
                "AI-анализ текста не настроен. Используйте строки: "
                "Название; масса или описание порции; ккал; белки; жиры; углеводы"
            )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.ai.analyze_text, text),
                timeout=self.ai.timeout_seconds + 3,
            )
        except (NutritionAIUnavailable, asyncio.TimeoutError) as exc:
            raise ValueError(
                "AI не ответил. Используйте структурный формат с КБЖУ."
            ) from exc
        return await self._accept_ai_result(update, context, result, source=source, description=text)

    async def _accept_ai_result(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        result: dict[str, Any],
        *,
        source: str,
        photo_file_id: str | None = None,
        description: str = "",
    ) -> bool:
        questions = result.get("questions") or []
        if questions:
            context.user_data["nutrition_state"] = "clarify_ai"
            context.user_data["nutrition_pending_description"] = (
                description or self._items_as_description(result["items"])
            )
            context.user_data["nutrition_pending_source"] = source
            context.user_data["nutrition_pending_photo_file_id"] = photo_file_id
            await self._reply(
                update,
                "Нужно уточнение перед созданием черновика:\n" +
                "\n".join(f"• {question}" for question in questions),
            )
            return False
        items = []
        for raw in result["items"]:
            item = dict(raw)
            item["calculation_method"] = "ai"
            item["approximate"] = True
            items.append(item)
        await self._create_draft(update, context, items, source=source, photo_file_id=photo_file_id)
        return True

    async def _create_draft(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        items: list[dict[str, Any]],
        *,
        source: str,
        photo_file_id: str | None = None,
    ) -> None:
        if source == "photo" and photo_file_id is None:
            photo_file_id = context.user_data.pop("nutrition_pending_photo_file_id", None)
        meal = self.store.create_meal_draft(
            client_telegram_id=update.effective_user.id,
            source=source,
            eaten_at=datetime.now(timezone.utc),
            meal_type=self._meal_type(update.effective_user.id),
            items=items,
            photo_file_id=photo_file_id,
        )
        await self._show_draft(update, context, meal)

    async def _show_reference_candidates(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        candidates: list[Any],
        *,
        query: str,
    ) -> None:
        context.user_data.pop("nutrition_reference_candidates", None)
        back = self._reference_back_callback(context)
        if not candidates:
            context.user_data["nutrition_state"] = "reference_search"
            await self._reply(
                update,
                f"По запросу «{html.escape(query[:120])}» нет подходящей записи. "
                "Уточните продукт, жирность и приготовление или вернитесь к ручному вводу.",
                parse_mode="HTML",
                reply_markup=self._cancel_keyboard(back),
            )
            return
        allowed = [candidate.fdc_id for candidate in candidates]
        context.user_data["nutrition_reference_candidates"] = allowed
        context.user_data["nutrition_reference_query"] = query
        buttons = []
        lines = ["Выберите конкретную запись справочника:"]
        for candidate in candidates:
            preparation = f", {candidate.preparation}" if candidate.preparation else ""
            label = f"{candidate.display_name}{preparation}"[:60]
            buttons.append([
                InlineKeyboardButton(label, callback_data=f"nutrition:ref_pick:{candidate.fdc_id}")
            ])
            lines.append(
                f"• <b>{html.escape(candidate.display_name)}</b>{html.escape(preparation)}\n"
                f"  {html.escape(candidate.description[:180])}"
            )
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=back)])
        buttons.append([InlineKeyboardButton("Отмена", callback_data="nutrition:menu")])
        await self._reply(
            update, "\n\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @staticmethod
    def _reference_back_callback(context: ContextTypes.DEFAULT_TYPE) -> str:
        if context.user_data.get("nutrition_reference_mode") == "refine":
            return f"nutrition:refine:{context.user_data.get('nutrition_reference_meal_id')}"
        if context.user_data.get("nutrition_reference_items"):
            return "nutrition:ref_review"
        return "nutrition:add_meal"

    async def _pick_reference_candidate(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, fdc_id: str
    ) -> None:
        if self.reference is None:
            raise ValueError("Справочник временно недоступен")
        allowed = context.user_data.get("nutrition_reference_candidates") or []
        if fdc_id not in allowed:
            raise PermissionError("Этот вариант поиска уже недоступен, выполните поиск снова")
        record = self.reference.get(fdc_id)
        context.user_data["nutrition_reference_fdc_id"] = record.fdc_id
        context.user_data["nutrition_state"] = "reference_grams"
        preparation = f" ({record.preparation})" if record.preparation else ""
        await update.callback_query.edit_message_text(
            f"Вы выбрали: <b>{html.escape(record.display_name)}</b>"
            f"{html.escape(preparation)}.\nВведите массу в граммах:",
            parse_mode="HTML",
            reply_markup=self._cancel_keyboard("nutrition:ref_back_candidates"),
        )

    async def _show_reference_builder(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        items = context.user_data.get("nutrition_reference_items") or []
        context.user_data["nutrition_state"] = "reference_review"
        lines = ["📚 <b>Продукты по справочнику</b>"]
        for item in items:
            lines.append(
                f"• {html.escape(item['name'])}, {item['weight_g']:g} г: "
                f"{item['calories']:g} ккал, Б {item['protein_g']:g}, "
                f"Ж {item['fat_g']:g}, У {item['carbs_g']:g}"
            )
        await self._reply(
            update, "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Ещё продукт", callback_data="nutrition:ref_more")],
                [InlineKeyboardButton("✅ Создать черновик", callback_data="nutrition:ref_finish")],
                [InlineKeyboardButton("Отмена", callback_data="nutrition:menu")],
            ]),
        )

    async def _show_refine_items(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, meal_id: int
    ) -> None:
        if self.reference is None:
            raise ValueError("Справочник временно недоступен")
        meal = self.store.get_owned_draft(
            client_telegram_id=update.effective_user.id, meal_id=meal_id
        )
        context.user_data["nutrition_reference_meal_id"] = meal_id
        buttons = []
        for item in meal["items"]:
            buttons.append([
                InlineKeyboardButton(
                    f"📚 {item['name']}"[:60],
                    callback_data=f"nutrition:refine_item:{item['id']}",
                )
            ])
            if item["calculation_method"] == "reference":
                buttons.append([
                    InlineKeyboardButton(
                        f"⚖️ Изменить массу: {item['name']}"[:60],
                        callback_data=f"nutrition:ref_weight:{item['id']}",
                    )
                ])
        buttons.append([
            InlineKeyboardButton("⬅️ К черновику", callback_data=f"nutrition:draft:{meal_id}")
        ])
        await update.callback_query.edit_message_text(
            "Выберите продукт, который нужно сверить с реальной записью справочника. "
            "Жирность и приготовление должны совпадать:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _search_for_draft_item(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, item_id: int
    ) -> None:
        if self.reference is None:
            raise ValueError("Справочник временно недоступен")
        meal_id = int(context.user_data.get("nutrition_reference_meal_id", 0))
        meal = self.store.get_owned_draft(
            client_telegram_id=update.effective_user.id, meal_id=meal_id
        )
        item = next((row for row in meal["items"] if row["id"] == item_id), None)
        if item is None:
            raise PermissionError("Продукт принадлежит другому приему или уже изменен")
        context.user_data["nutrition_reference_item_id"] = item_id
        context.user_data["nutrition_reference_mode"] = "refine"
        candidates = self.reference.search(item["name"], limit=5)
        await self._show_reference_candidates(update, context, candidates, query=item["name"])

    @staticmethod
    def _reference_item(calculated: Any) -> dict[str, Any]:
        return {
            "name": calculated.display_name,
            "weight_g": float(calculated.grams),
            "portion_text": "",
            "calories": float(calculated.totals.kcal),
            "protein_g": float(calculated.totals.protein_g),
            "fat_g": float(calculated.totals.fat_g),
            "carbs_g": float(calculated.totals.carbs_g),
            "approximate": False,
            "calculation_method": "reference",
            "reference_fdc_id": calculated.fdc_id,
            "reference_source": calculated.source,
            "reference_version": calculated.version,
            "reference_url": calculated.url,
            "reference_description": calculated.description,
            "reference_preparation": calculated.preparation,
            "reference_kcal_per_100g": float(calculated.per_100g.kcal),
            "reference_protein_per_100g": float(calculated.per_100g.protein_g),
            "reference_fat_per_100g": float(calculated.per_100g.fat_g),
            "reference_carbs_per_100g": float(calculated.per_100g.carbs_g),
        }

    @staticmethod
    def _clear_reference_context(
        context: ContextTypes.DEFAULT_TYPE, *, keep_state: bool = False
    ) -> None:
        for key in list(context.user_data):
            if key.startswith("nutrition_reference_"):
                context.user_data.pop(key, None)
        if not keep_state:
            context.user_data.pop("nutrition_state", None)

    @staticmethod
    def _cancel_keyboard(back_callback: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)],
            [InlineKeyboardButton("Отмена", callback_data="nutrition:menu")],
        ])

    async def _show_draft(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        meal: dict[str, Any],
    ) -> None:
        context.user_data["nutrition_state"] = "draft"
        lines = [f"🍽️ <b>Черновик #{meal['id']}</b>"]
        approximate = False
        for item in meal["items"]:
            portion = f"{item['weight_g']:g} г" if item["weight_g"] else item["portion_text"]
            approximate = approximate or bool(item["approximate"])
            lines.append(
                f"• {html.escape(item['name'])}, {html.escape(portion)}: "
                f"{item['calories']:g} ккал, Б {item['protein_g']:g}, "
                f"Ж {item['fat_g']:g}, У {item['carbs_g']:g}"
            )
            if item.get("calculation_method") == "reference":
                prep = f", {item['reference_preparation']}" if item.get("reference_preparation") else ""
                lines.append(
                    f"  📚 По справочнику: {html.escape(item['reference_source'])}, "
                    f"версия {html.escape(item['reference_version'])}{html.escape(prep)}"
                )
        if approximate:
            lines.append("\n⚠️ Оценка приблизительная. Проверьте блюдо и размер порции.")
        lines.append("\nЗапись попадет в статистику только после подтверждения.")
        confirm_label = "✅ Сохранить приблизительную оценку" if approximate else "✅ Подтвердить"
        keyboard = []
        if self.reference is not None:
            keyboard.append([
                InlineKeyboardButton("📚 Подобрать в справочнике", callback_data=f"nutrition:refine:{meal['id']}")
            ])
        keyboard.extend([
            [InlineKeyboardButton(confirm_label, callback_data=f"nutrition:confirm:{meal['id']}")],
            [InlineKeyboardButton("✏️ Исправить вручную", callback_data=f"nutrition:edit:{meal['id']}")],
            [InlineKeyboardButton("Отменить", callback_data=f"nutrition:cancel:{meal['id']}")],
        ])
        await self._reply(
            update,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _send_menu(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        edit: bool = False,
    ) -> None:
        buttons = [
            [InlineKeyboardButton("🍽️ Добавить прием пищи", callback_data="nutrition:add_meal")],
            [InlineKeyboardButton("💧 Добавить воду", callback_data="nutrition:water")],
            [
                InlineKeyboardButton("📊 Сегодня", callback_data="nutrition:today"),
                InlineKeyboardButton("📅 Неделя", callback_data="nutrition:week"),
            ],
            [InlineKeyboardButton("🔗 Ввести код тренера", callback_data="nutrition:link")],
            [InlineKeyboardButton("🕒 Часовой пояс", callback_data="nutrition:timezone")],
            [InlineKeyboardButton("Отозвать согласие на фото", callback_data="nutrition:consent_revoke")],
            [InlineKeyboardButton("Отключить тренера", callback_data="nutrition:unlink")],
        ]
        if self.is_trainer(update.effective_user.id):
            buttons.append([InlineKeyboardButton("👥 Кабинет тренера", callback_data="nutrition:trainer")])
        buttons.append([InlineKeyboardButton("Закрыть дневник", callback_data="nutrition:leave")])
        text = (
            "🥗 <b>Дневник питания</b>\n\n"
            "Фото и текст создают черновик. AI-оценка приблизительная и требует "
            "вашего подтверждения. Бот не делает медицинских назначений."
        )
        markup = InlineKeyboardMarkup(buttons)
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await self._reply(update, text, parse_mode="HTML", reply_markup=markup)

    async def _show_trainer_clients(self, update: Update) -> None:
        clients = self.store.list_trainer_clients(update.effective_user.id)
        keyboard = [
            [InlineKeyboardButton(client["display_name"], callback_data=f"nutrition:trainer_day:{client['id']}")]
            for client in clients
        ]
        keyboard.append([InlineKeyboardButton("🔑 Код для клиентов", callback_data="nutrition:invite")])
        if self.dashboard_origin:
            keyboard.append(
                [InlineKeyboardButton("🌐 Веб-панель", web_app=WebAppInfo(url=self.dashboard_origin))]
            )
        keyboard.append([InlineKeyboardButton("⬅️ В дневник", callback_data="nutrition:menu")])
        message = "👥 <b>Клиенты тренера</b>"
        if not clients:
            message += "\n\nПока нет привязанных клиентов. Покажите постоянный код клиенту."
        await update.callback_query.edit_message_text(
            message, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _show_trainer_meal_picker(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        client_id: int,
        *,
        action: str,
    ) -> None:
        self._clear_pending_trainer_action(context)
        meals = self.store.list_recent_client_meals(update.effective_user.id, client_id)
        prefix = "tcm" if action == "comment" else "tem"
        buttons = []
        for meal in meals:
            label = self._meal_button_label(meal)
            buttons.append([
                InlineKeyboardButton(label, callback_data=f"nutrition:{prefix}:{meal['id']}")
            ])
        buttons.append([
            InlineKeyboardButton("⬅️ К клиенту", callback_data=f"nutrition:trainer_day:{client_id}")
        ])
        buttons.append([InlineKeyboardButton("Отмена", callback_data="nutrition:trainer")])
        action_text = "комментария" if action == "comment" else "исправления"
        message = f"Выберите запись для {action_text}:"
        if not meals:
            message = "У клиента пока нет подтвержденных записей."
        await update.callback_query.edit_message_text(
            message, reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def _start_norms_wizard(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        client_id: int,
    ) -> None:
        self._require_trainer(update.effective_user.id)
        today = self._today_for_client(update.effective_user.id, client_id)
        summary = self.store.get_client_day(update.effective_user.id, client_id, today)
        self._clear_pending_trainer_action(context)
        context.user_data["nutrition_trainer_client_id"] = client_id
        context.user_data["nutrition_norms_draft"] = {"effective_from": None}
        context.user_data["nutrition_norms_prior"] = summary.get("norms") or {}
        context.user_data["nutrition_norms_nonce"] = self._new_flow_nonce()
        await self._show_norms_date(update, context)

    async def _show_norms_date(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        client_id = int(context.user_data.get("nutrition_trainer_client_id", 0))
        nonce = str(context.user_data.get("nutrition_norms_nonce", ""))
        self._require_flow_session(context, "norms", client_id, nonce)
        draft = context.user_data.get("nutrition_norms_draft") or {}
        selected = draft.get("effective_from")
        selected_text = f"\n\nСейчас выбрано: {selected}." if selected else ""
        context.user_data["nutrition_state"] = "norms_date"
        await update.callback_query.edit_message_text(
            "С какой даты начать дневные нормы? Они действуют с этой даты до следующей "
            "записи норм. Бот сохраняет ваши значения и не рассчитывает медицинские рекомендации."
            + selected_text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "Сегодня", callback_data=f"nutrition:norms_date:{client_id}:{nonce}:today"
                    ),
                    InlineKeyboardButton(
                        "Завтра", callback_data=f"nutrition:norms_date:{client_id}:{nonce}:tomorrow"
                    ),
                ],
                [InlineKeyboardButton(
                    "Другая дата", callback_data=f"nutrition:norms_date:{client_id}:{nonce}:other"
                )],
                [InlineKeyboardButton("⬅️ К клиенту", callback_data=f"nutrition:trainer_day:{client_id}")],
                [InlineKeyboardButton("Отмена", callback_data="nutrition:trainer")],
            ]),
        )

    async def _show_norms_step(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        step: int,
    ) -> None:
        client_id = int(context.user_data.get("nutrition_trainer_client_id", 0))
        self._require_trainer(update.effective_user.id)
        self.store.get_client_day(
            update.effective_user.id, client_id,
            self._today_for_client(update.effective_user.id, client_id),
        )
        draft = context.user_data.get("nutrition_norms_draft") or {}
        nonce = str(context.user_data.get("nutrition_norms_nonce", ""))
        self._require_flow_session(context, "norms", client_id, nonce)
        if step >= len(self._NORMS_FIELDS):
            if not any(draft.get(key) is not None for key, _, _ in self._NORMS_FIELDS):
                raise ValueError("Укажите хотя бы одну дневную норму")
            context.user_data["nutrition_state"] = "norms_preview"
            context.user_data["nutrition_norms_step"] = len(self._NORMS_FIELDS)
            lines = [f"Проверьте дневные нормы с {draft['effective_from']}:"]
            for key, label, unit in self._NORMS_FIELDS:
                value = draft.get(key)
                shown = "не задано" if value is None else f"{value:g} {unit}"
                lines.append(f"• {label}: {shown}")
            lines.append("\nДругие даты норм не удаляются.")
            await self._reply(
                update, "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ Сохранить",
                        callback_data=f"nutrition:norms_confirm:{client_id}:{nonce}",
                    )],
                    [InlineKeyboardButton(
                        "⬅️ Назад", callback_data=f"nutrition:norms_back:{client_id}:{nonce}"
                    )],
                    [InlineKeyboardButton("Отмена", callback_data=f"nutrition:trainer_day:{client_id}")],
                ]),
            )
            return
        context.user_data["nutrition_norms_step"] = step
        context.user_data["nutrition_state"] = "norms_value"
        key, label, unit = self._NORMS_FIELDS[step]
        prior = (context.user_data.get("nutrition_norms_prior") or {}).get(key)
        buttons = []
        if key in draft:
            entered = draft[key]
            entered_text = "не задано" if entered is None else f"{entered:g} {unit}"
            buttons.append([InlineKeyboardButton(
                f"Оставить введенное: {entered_text}",
                callback_data=f"nutrition:norms_choice:{client_id}:{nonce}:keep_draft",
            )])
        if prior is not None:
            buttons.append([
                InlineKeyboardButton(
                    f"Оставить текущее: {prior:g}",
                    callback_data=f"nutrition:norms_choice:{client_id}:{nonce}:keep",
                )
            ])
        buttons.append([
            InlineKeyboardButton(
                "Не задавать",
                callback_data=f"nutrition:norms_choice:{client_id}:{nonce}:none",
            )
        ])
        buttons.append([InlineKeyboardButton(
            "⬅️ Назад", callback_data=f"nutrition:norms_back:{client_id}:{nonce}"
        )])
        buttons.append([
            InlineKeyboardButton("Отмена", callback_data=f"nutrition:trainer_day:{client_id}")
        ])
        await self._reply(
            update,
            f"{label}: введите значение, единица измерения {unit}. "
            "Это норма на один день (сутки).",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _apply_norms_choice(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        choice: str,
    ) -> None:
        step = int(context.user_data.get("nutrition_norms_step", 0))
        key, _, _ = self._NORMS_FIELDS[step]
        if choice == "keep_draft":
            draft = context.user_data.get("nutrition_norms_draft") or {}
            if key not in draft:
                raise ValueError("Введенное значение для этого поля уже недоступно")
            value = draft[key]
        elif choice == "keep":
            prior = (context.user_data.get("nutrition_norms_prior") or {}).get(key)
            if prior is None:
                raise ValueError("Текущее значение для этого поля не задано")
            value = prior
        elif choice == "none":
            value = None
        else:
            raise ValueError("Неизвестный выбор нормы")
        context.user_data["nutrition_norms_draft"][key] = value
        await self._show_norms_step(update, context, step + 1)

    @staticmethod
    def _meal_button_label(meal: dict[str, Any]) -> str:
        moment = datetime.fromisoformat(meal["eaten_at"]).astimezone(ZoneInfo(meal["timezone"]))
        names = ", ".join(item["name"] for item in meal["items"][:2]) or meal["meal_type"]
        return f"{moment:%d.%m %H:%M} · {names}"[:60]

    @staticmethod
    def _meal_context(meal: dict[str, Any], *, include_comments: bool = False) -> str:
        moment = datetime.fromisoformat(meal["eaten_at"]).astimezone(ZoneInfo(meal["timezone"]))
        lines = [
            f"🍽️ <b>{html.escape(meal['meal_type'])}, {moment:%d.%m.%Y %H:%M}</b>"
        ]
        for item in meal["items"]:
            lines.append(
                f"• {html.escape(item['name'])}: {item['calories']:g} ккал, "
                f"Б {item['protein_g']:g}, Ж {item['fat_g']:g}, У {item['carbs_g']:g}"
            )
        if include_comments:
            for comment in meal["comments"]:
                lines.append(f"💬 {html.escape(comment['text'])}")
        return "\n".join(lines)

    @staticmethod
    def _clear_pending_trainer_action(context: ContextTypes.DEFAULT_TYPE) -> None:
        for key in (
            "nutrition_state", "nutrition_trainer_meal_id", "nutrition_norms_draft",
            "nutrition_norms_step", "nutrition_norms_prior", "nutrition_pending_plan",
            "nutrition_norms_nonce", "nutrition_plan_nonce", "nutrition_trainer_client_id",
        ):
            context.user_data.pop(key, None)

    @staticmethod
    def _new_flow_nonce() -> str:
        return secrets.token_urlsafe(6)

    @staticmethod
    def _require_flow_session(
        context: ContextTypes.DEFAULT_TYPE,
        kind: str,
        client_id: int,
        nonce: str,
        *,
        expected_state: str | None = None,
    ) -> None:
        if (
            int(context.user_data.get("nutrition_trainer_client_id", 0)) != client_id
            or context.user_data.get(f"nutrition_{kind}_nonce") != nonce
            or (expected_state is not None and context.user_data.get("nutrition_state") != expected_state)
        ):
            raise PermissionError(
                "Этот экран уже устарел. Откройте клиента и начните действие заново."
            )

    @staticmethod
    def _trainer_client_keyboard(client_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Сегодня", callback_data=f"nutrition:trainer_day:{client_id}"),
                    InlineKeyboardButton("Неделя", callback_data=f"nutrition:trainer_week:{client_id}"),
                ],
                [InlineKeyboardButton("🎯 Задать нормы", callback_data=f"nutrition:trainer_norms:{client_id}")],
                [InlineKeyboardButton("📎 Загрузить план XLSX", callback_data=f"nutrition:trainer_plan_upload:{client_id}")],
                [InlineKeyboardButton("📄 Скачать шаблон", callback_data=f"nutrition:trainer_plan_template:{client_id}")],
                [InlineKeyboardButton("✏️ Исправить прием", callback_data=f"nutrition:trainer_edit:{client_id}")],
                [InlineKeyboardButton("💬 Комментарий", callback_data=f"nutrition:trainer_comment:{client_id}")],
                [InlineKeyboardButton("⬅️ Клиенты", callback_data="nutrition:trainer")],
            ]
        )

    @staticmethod
    def parse_structured_items(text: str) -> list[dict[str, Any]]:
        items = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split(";")]
            if len(parts) != 6:
                raise ValueError(
                    "В каждой строке нужно 6 полей: название; масса/порция; ккал; Б; Ж; У"
                )
            name, portion_raw, calories, protein, fat, carbs = parts
            try:
                weight = float(portion_raw.replace(",", "."))
                portion_text = ""
                approximate = False
            except ValueError:
                weight = None
                portion_text = portion_raw
                approximate = True
            if weight is None and not portion_text:
                raise ValueError(f"Нужна масса или порция для блюда {name}")
            items.append(
                {
                    "name": name,
                    "weight_g": weight,
                    "portion_text": portion_text,
                    "calories": float(calories.replace(",", ".")),
                    "protein_g": float(protein.replace(",", ".")),
                    "fat_g": float(fat.replace(",", ".")),
                    "carbs_g": float(carbs.replace(",", ".")),
                    "approximate": approximate,
                }
            )
        if not items:
            raise ValueError("Нужен хотя бы один продукт или блюдо")
        return items

    @staticmethod
    def parse_norms(text: str) -> tuple[str, dict[str, Any]]:
        parts = [part.strip() for part in text.split(";")]
        if len(parts) != 6:
            raise ValueError("Формат: YYYY-MM-DD; ккал; белки; жиры; углеводы; вода мл")
        effective_from, calories, protein, fat, carbs, water = parts
        return effective_from, {
            "calories": float(calories.replace(",", ".")),
            "protein_g": float(protein.replace(",", ".")),
            "fat_g": float(fat.replace(",", ".")),
            "carbs_g": float(carbs.replace(",", ".")),
            "water_ml": int(water),
        }

    def _ensure_user(self, update: Update) -> dict[str, Any]:
        user = update.effective_user
        return self.store.ensure_user(
            telegram_id=user.id,
            display_name=user.full_name or user.first_name or "Пользователь",
        )

    def _require_trainer(self, telegram_id: int) -> None:
        if not self.is_trainer(telegram_id):
            raise PermissionError("Доступно только тренеру из списка доступа")

    def _today(self, telegram_id: int):
        user = self.store.get_user_by_telegram_id(telegram_id)
        return datetime.now(ZoneInfo(user["timezone"])).date()

    def _today_for_client(self, trainer_telegram_id: int, client_id: int):
        clients = self.store.list_trainer_clients(trainer_telegram_id)
        client = next((item for item in clients if item["id"] == client_id), None)
        if client is None:
            raise PermissionError("Клиент не найден")
        return datetime.now(ZoneInfo(client["timezone"])).date()

    def _meal_type(self, telegram_id: int) -> str:
        user = self.store.get_user_by_telegram_id(telegram_id)
        hour = datetime.now(ZoneInfo(user["timezone"])).hour
        if 5 <= hour < 11:
            return "завтрак"
        if 11 <= hour < 16:
            return "обед"
        if 17 <= hour < 23:
            return "ужин"
        return "перекус"

    @staticmethod
    def _format_day(summary: dict[str, Any], trainer: bool = False) -> str:
        client = summary["client"]
        title = f"📊 <b>{html.escape(client['display_name'])}, {summary['date']}</b>" if trainer else f"📊 <b>Сегодня, {summary['date']}</b>"
        lines = [title]
        for meal in summary["meals"]:
            total = sum(float(item["calories"]) for item in meal["items"])
            lines.append(f"• {html.escape(meal['meal_type'])}: {total:g} ккал")
            if trainer:
                for item in meal["items"]:
                    lines.append(f"  · {html.escape(item['name'])}")
                for comment in meal["comments"]:
                    lines.append(f"  💬 {html.escape(comment['text'])}")
        if not summary["meals"]:
            lines.append("Записей о приемах пищи нет. Это не означает, что человек не ел.")
        totals = summary["totals"]
        lines.append(
            f"\nИтого по записям: {totals['calories']:g} ккал, "
            f"Б {totals['protein_g']:g}, Ж {totals['fat_g']:g}, У {totals['carbs_g']:g}"
        )
        lines.append(f"💧 Вода по записям: {summary['water_ml']} мл")
        norms = summary.get("norms")
        if norms:
            def shown(key: str) -> str:
                value = norms[key]
                return "не задано" if value is None else f"{value:g}"

            lines.append(
                f"Нормы с {norms['effective_from']}: {shown('calories')} ккал/сутки, "
                f"Б {shown('protein_g')} г/сутки, Ж {shown('fat_g')} г/сутки, "
                f"У {shown('carbs_g')} г/сутки, вода {shown('water_ml')} мл/сутки"
            )
        else:
            lines.append("Нормы пока не заданы тренером.")
        return "\n".join(lines)

    @staticmethod
    def _format_week(summary: dict[str, Any]) -> str:
        lines = [f"📅 <b>Неделя с {summary['week_start']}</b>"]
        for day in summary["days"]:
            totals = day["totals"]
            marker = "нет записей" if not day["meals"] else f"{totals['calories']:g} ккал"
            lines.append(f"• {day['date']}: {marker}, вода {day['water_ml']} мл")
        lines.append(
            "\nСреднее за 7 календарных дней рассчитано только из внесенных записей. "
            "Пустой день не означает отсутствие еды."
        )
        averages = summary["averages"]
        lines.append(
            f"Среднее по записям: {averages['calories']:g} ккал, "
            f"Б {averages['protein_g']:g}, Ж {averages['fat_g']:g}, "
            f"У {averages['carbs_g']:g}, вода {averages['water_ml']:g} мл"
        )
        return "\n".join(lines)

    @staticmethod
    async def _reply(update: Update, text: str, **kwargs: Any) -> None:
        if update.callback_query:
            await update.callback_query.message.reply_text(text, **kwargs)
        else:
            await update.message.reply_text(text, **kwargs)

    @staticmethod
    async def _private_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        chat = update.effective_chat
        if chat is not None and chat.type == "private":
            return True
        username = os.getenv("TELEGRAM_BOT_USERNAME", "natrium_smm_bot").lstrip("@")
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Открыть личный чат", url=f"https://t.me/{username}?start=nutrition")]]
        )
        message = (
            "Дневник питания доступен только в личном чате с ботом. "
            "Личные записи и фото в группе не обрабатываются."
        )
        if update.effective_message:
            await update.effective_message.reply_text(message, reply_markup=markup)
        return False

    @staticmethod
    def _manual_prompt() -> str:
        return (
            "Опишите еду обычным текстом. Также можно ввести точно, каждое блюдо с новой строки:\n"
            "Название; масса в г или описание порции; ккал; белки; жиры; углеводы"
        )

    @staticmethod
    def _items_as_description(items: list[dict[str, Any]]) -> str:
        return "; ".join(
            f"{item.get('name')}, {item.get('weight_g') or item.get('portion_text')}"
            for item in items
        )

    @staticmethod
    def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
        for key in list(context.user_data):
            if key.startswith("nutrition_"):
                context.user_data.pop(key, None)
