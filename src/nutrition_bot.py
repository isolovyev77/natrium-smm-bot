"""Telegram-сценарии дневника питания.

Контроллер подключается к существующему TelegramSMMBot и обрабатывает только
callback с префиксом ``nutrition:`` и сообщения активной nutrition-сессии.
Все записи сначала создаются как черновик и попадают в статистику лишь после
явного подтверждения пользователя.
"""

from __future__ import annotations

import asyncio
import copy
import html
import math
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from src.nutrition_ai import NutritionAI, NutritionAIResponseError, NutritionAIUnavailable
from src.nutrition_help import NutritionHelp, NutritionHelpContext, is_help_question
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
    _MANUAL_FIELDS = (
        ("name", "Название продукта", "например: Рис вареный"),
        ("weight_g", "Масса всей порции", "г, например: 180"),
        ("calories", "Калории всей порции", "ккал, например: 230"),
        ("protein_g", "Белки всей порции", "г, например: 5.4"),
        ("fat_g", "Жиры всей порции", "г, например: 1.2"),
        ("carbs_g", "Углеводы всей порции", "г, например: 48"),
    )
    _PROFILE_FIELDS = (
        ("display_name", "Имя", "например: Анна"),
        ("height_cm", "Рост", "см, например: 172"),
        ("goal", "Цель", "например: поддерживать режим питания"),
        ("timezone", "Часовой пояс", "например: Europe/Moscow"),
    )
    _MOODS = {
        "great": "Отличное",
        "good": "Хорошее",
        "neutral": "Нейтральное",
        "low": "Пониженное",
        "stressed": "Напряженное",
    }

    def __init__(
        self,
        *,
        store: NutritionStore,
        ai: NutritionAI | None = None,
        trainer_ids: set[int] | None = None,
        dashboard_origin: str | None = None,
        reference: FoodReference | None = None,
        help_service: NutritionHelp | None = None,
    ):
        self.store = store
        self.ai = ai or NutritionAI()
        self.trainer_ids = trainer_ids if trainer_ids is not None else parse_trainer_ids()
        self.help_service = help_service or NutritionHelp()
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
        if data not in {"nutrition:help", "nutrition:help_resume"}:
            context.user_data.pop("nutrition_help_return", None)
        try:
            if data == "nutrition:help":
                await self._enter_help(update, context)
            elif data == "nutrition:help_resume":
                await self._resume_help(update, context)
            elif data == "nutrition:menu":
                self._clear(context)
                context.user_data["nutrition_active"] = True
                await self._send_menu(update, context, edit=True)
            elif data == "nutrition:add_meal":
                self._clear_reference_context(context)
                self._clear_manual_flow(context)
                self._clear_mealctx_flow(context)
                keyboard = [
                    [InlineKeyboardButton("📚 Рассчитать по справочнику", callback_data="nutrition:meal_reference")],
                    [InlineKeyboardButton("📸 Фото еды", callback_data="nutrition:meal_photo")],
                    [InlineKeyboardButton("✍️ Описание с помощью ИИ", callback_data="nutrition:meal_text_ai")],
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
                await self._start_manual_wizard(update, context)
            elif data == "nutrition:meal_text_ai":
                context.user_data["nutrition_state"] = "wait_manual"
                await query.edit_message_text(
                    "Ваше описание будет передано искусственному интеллекту для приблизительной оценки. "
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
                await query.edit_message_text(
                    "Фото не отправлено искусственному интеллекту. Перейдем к пошаговому "
                    "ручному вводу.",
                )
                await self._start_manual_wizard(update, context)
            elif data == "nutrition:consent_revoke":
                self.store.revoke_photo_consent(telegram_id=update.effective_user.id)
                context.user_data.pop("nutrition_pending_photo", None)
                await query.edit_message_text(
                    "Согласие отозвано. Новые фото не будут передаваться искусственному "
                    "интеллекту без нового согласия. Ручной ввод остается доступен.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🥗 В дневник", callback_data="nutrition:menu")]
                    ]),
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
            elif data == "nutrition:profile":
                self._clear_profile_flow(context)
                await self._show_profile(update, context)
            elif data == "nutrition:profile_edit":
                await self._start_profile_wizard(update, context)
            elif data.startswith("nutrition:profile_choice:"):
                _, _, nonce, choice = data.split(":", 3)
                self._require_self_flow(context, "profile", nonce, "profile_value")
                await self._apply_profile_choice(update, context, choice)
            elif data.startswith("nutrition:profile_back:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "profile", nonce)
                step = int(context.user_data.get("nutrition_profile_step", 0))
                if step <= 0:
                    self._clear_profile_flow(context)
                    await self._show_profile(update, context)
                else:
                    await self._show_profile_step(update, context, step - 1)
            elif data.startswith("nutrition:profile_confirm:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "profile", nonce, "profile_preview")
                await self._confirm_profile(update, context)
            elif data == "nutrition:weight":
                self._clear_weight_flow(context)
                await self._show_weight_menu(update, context)
            elif data == "nutrition:reminders":
                await self._show_client_reminders(update, context)
            elif data.startswith("nutrition:clientrem_start:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "clientrem", nonce, "clientrem_menu")
                await self._show_client_reminder_step(update, context, 0)
            elif data.startswith("nutrition:clientrem_choice:"):
                _, _, nonce, choice = data.split(":", 3)
                self._require_self_flow(context, "clientrem", nonce)
                await self._client_reminder_choice(update, context, choice)
            elif data.startswith("nutrition:clientrem_back:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "clientrem", nonce)
                await self._back_client_reminder(update, context)
            elif data.startswith("nutrition:clientrem_confirm:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "clientrem", nonce, "clientrem_preview")
                await self._confirm_client_reminders(update, context)
            elif data == "nutrition:trainer_reminders":
                self._require_trainer(update.effective_user.id)
                await self._show_trainer_reminders(update, context)
            elif data.startswith("nutrition:trainerrem_start:"):
                self._require_trainer(update.effective_user.id)
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "trainerrem", nonce, "trainerrem_menu")
                context.user_data["nutrition_trainerrem_draft"]["enabled"] = True
                context.user_data["nutrition_trainerrem_draft"]["daily_digest"]["enabled"] = True
                await self._show_trainer_reminder_step(update, context, 0)
            elif data.startswith("nutrition:trainerrem_choice:"):
                self._require_trainer(update.effective_user.id)
                _, _, nonce, choice = data.split(":", 3)
                self._require_self_flow(context, "trainerrem", nonce)
                await self._trainer_reminder_choice(update, context, choice)
            elif data.startswith("nutrition:trainerrem_confirm:"):
                self._require_trainer(update.effective_user.id)
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "trainerrem", nonce, "trainerrem_preview")
                await self._confirm_trainer_reminders(update, context)
            elif data == "nutrition:weight_add":
                await self._start_weight_wizard(update, context)
            elif data.startswith("nutrition:weight_date:"):
                _, _, nonce, choice = data.split(":", 3)
                self._require_self_flow(context, "weight", nonce, "weight_date")
                if choice == "other":
                    context.user_data["nutrition_state"] = "weight_date_other"
                    await self._reply(
                        update, "Введите дату измерения ГГГГ-ММ-ДД:",
                        reply_markup=self._weight_footer(nonce),
                    )
                else:
                    if choice == "keep_date":
                        if not context.user_data["nutrition_weight_draft"].get("local_date"):
                            raise PermissionError("Дата этого шага больше недоступна")
                        await self._show_weight_time(update, context)
                        return
                    if choice not in {"today", "yesterday"}:
                        raise ValueError("Неизвестный вариант даты")
                    today = self._today(update.effective_user.id)
                    chosen = today if choice == "today" else today - timedelta(days=1)
                    context.user_data["nutrition_weight_draft"]["local_date"] = chosen.isoformat()
                    await self._show_weight_time(update, context)
            elif data.startswith("nutrition:weight_keep:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "weight", nonce, "weight_value")
                if "weight_kg" not in context.user_data.get("nutrition_weight_draft", {}):
                    raise PermissionError("Значение этого шага больше недоступно")
                await self._show_weight_date(update, context)
            elif data.startswith("nutrition:weight_time_keep:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "weight", nonce, "weight_time")
                draft = context.user_data.get("nutrition_weight_draft") or {}
                if not draft.get("local_time") or not draft.get("measured_at"):
                    raise PermissionError("Время этого шага больше недоступно")
                await self._accept_weight_time(update, context, str(draft["local_time"]))
            elif data.startswith("nutrition:weight_back:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "weight", nonce)
                await self._back_weight_wizard(update, context)
            elif data.startswith("nutrition:weight_note_skip:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "weight", nonce, "weight_note")
                context.user_data["nutrition_weight_draft"]["note"] = ""
                await self._show_weight_preview(update, context)
            elif data.startswith("nutrition:weight_confirm:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "weight", nonce, "weight_preview")
                await self._confirm_weight(update, context)
            elif data == "nutrition:weight_history":
                await self._show_weight_history(update, context)
            elif data == "nutrition:weight_day":
                context.user_data["nutrition_state"] = "weight_history_date"
                await query.edit_message_text(
                    "Введите дату записей веса в формате ГГГГ-ММ-ДД:",
                    reply_markup=self._cancel_keyboard("nutrition:weight"),
                )
            elif data.startswith("nutrition:weight_edit:"):
                _, _, date_value, weight_raw, version_raw = data.split(":", 4)
                await self._start_weight_edit(
                    update, context, date_value, int(weight_raw), int(version_raw)
                )
            elif data.startswith("nutrition:weight_cancel:") and not data.startswith(
                "nutrition:weight_cancel_confirm:"
            ):
                _, _, date_value, weight_raw, version_raw = data.split(":", 4)
                await self._confirm_weight_cancel_screen(
                    update, context, date_value, int(weight_raw), int(version_raw)
                )
            elif data.startswith("nutrition:weight_cancel_confirm:"):
                _, _, date_value, weight_raw, version_raw = data.split(":", 4)
                self.store.cancel_weight(
                    client_telegram_id=update.effective_user.id,
                    weight_id=int(weight_raw),
                    expected_version=int(version_raw),
                )
                await self._show_weight_day(update, context, date.fromisoformat(date_value))
            elif data.startswith("nutrition:meal_context:"):
                meal_id = int(data.rsplit(":", 1)[1])
                await self._start_meal_context(update, context, meal_id)
            elif data.startswith("nutrition:mealctx_choice:"):
                _, _, nonce, choice = data.split(":", 3)
                self._require_self_flow(context, "mealctx", nonce)
                await self._apply_meal_context_choice(update, context, choice)
            elif data.startswith("nutrition:mealctx_back:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "mealctx", nonce)
                await self._back_meal_context(update, context)
            elif data.startswith("nutrition:mealctx_confirm:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "mealctx", nonce, "mealctx_preview")
                await self._confirm_meal_context(update, context)
            elif data.startswith("nutrition:manual_back:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "manual", nonce)
                step = int(context.user_data.get("nutrition_manual_step", 0))
                if step <= 0:
                    self._clear_manual_flow(context)
                    await query.edit_message_text(
                        "Ручной ввод отменен.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("⬅️ К способам ввода", callback_data="nutrition:add_meal")]
                        ]),
                    )
                else:
                    await self._show_manual_step(update, context, step - 1)
            elif data.startswith("nutrition:manual_keep:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "manual", nonce, "manual_item")
                step = int(context.user_data.get("nutrition_manual_step", 0))
                key = self._MANUAL_FIELDS[step][0]
                if key not in context.user_data.get("nutrition_manual_item", {}):
                    raise ValueError("Ранее введенное значение недоступно")
                await self._show_manual_step(update, context, step + 1)
            elif data.startswith("nutrition:manual_confirm:"):
                nonce = data.rsplit(":", 1)[1]
                self._require_self_flow(context, "manual", nonce, "manual_preview")
                await self._confirm_manual_item(update, context)
            elif data == "nutrition:cabinet_link":
                await self._show_browser_cabinet_link(update, context)
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
                        [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
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
                        [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
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
        except RuntimeError:
            await query.message.reply_text(
                "⚠️ Данные уже изменились. Откройте текущий экран заново."
            )
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
                [InlineKeyboardButton("Без ИИ, введу вручную", callback_data="nutrition:consent_no")],
                [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:add_meal")],
                [InlineKeyboardButton("Отмена", callback_data="nutrition:menu")],
            ]
            await update.message.reply_text(
                "Для разбора фото оно будет передано искусственному интеллекту. Анализ создает "
                "приблизительный черновик, который не сохраняется без вашего подтверждения. "
                "Согласие сохранится для следующих фото, его можно отозвать в меню. "
                "Если вы не согласны, используйте ручной ввод. Передавать фото ИИ?",
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
        await self._show_pending_plan(update, context)
        return True

    async def _show_pending_plan(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        pending = context.user_data.get("nutrition_pending_plan") or []
        client_id = int(context.user_data.get("nutrition_trainer_client_id", 0))
        nonce = str(context.user_data.get("nutrition_plan_nonce", ""))
        self._require_flow_session(
            context, "plan", client_id, nonce, expected_state="trainer_plan_preview"
        )
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
        await self._reply(
            update,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "✅ Применить план",
                    callback_data=f"nutrition:trainer_plan_confirm:{client_id}:{nonce}",
                )],
                [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                [InlineKeyboardButton("⬅️ Загрузить другой файл", callback_data=f"nutrition:trainer_plan_upload:{client_id}")],
                [InlineKeyboardButton("Отмена", callback_data=f"nutrition:trainer_day:{client_id}")],
            ]),
        )

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if not self.is_active(context) or not update.message or not update.message.text:
            return False
        if not await self._private_only(update, context):
            return True
        self._ensure_user(update)
        state = context.user_data.get("nutrition_state")
        text = update.message.text.strip()
        try:
            if state == "nutrition_help_question":
                await self._answer_help(update, context, text)
            elif is_help_question(text, state=state):
                self._capture_help_return(context)
                await self._answer_help(update, context, text)
            elif state == "manual_item":
                await self._accept_manual_value(update, context, text)
            elif state == "profile_value":
                await self._accept_profile_value(update, context, text)
            elif state == "weight_value":
                value = self._positive_number(text, "Вес", maximum=1000)
                context.user_data["nutrition_weight_draft"]["weight_kg"] = value
                await self._show_weight_date(update, context)
            elif state == "weight_date_other":
                try:
                    chosen = date.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError("Введите дату в формате ГГГГ-ММ-ДД") from exc
                context.user_data["nutrition_weight_draft"]["local_date"] = chosen.isoformat()
                await self._show_weight_time(update, context)
            elif state == "weight_time":
                await self._accept_weight_time(update, context, text)
            elif state == "weight_note":
                if len(text) > 500:
                    raise ValueError("Заметка о весе должна быть короче 500 символов")
                context.user_data["nutrition_weight_draft"]["note"] = text
                await self._show_weight_preview(update, context)
            elif state == "weight_edit_value":
                value = self._positive_number(text, "Вес", maximum=1000)
                await self._apply_weight_edit(update, context, value)
            elif state == "weight_history_date":
                try:
                    chosen = date.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError("Введите дату в формате ГГГГ-ММ-ДД") from exc
                await self._show_weight_day(update, context, chosen)
            elif state == "clientrem_meal_times":
                times = self._parse_times(text, limit=3)
                context.user_data["nutrition_clientrem_draft"]["meal"] = {"enabled": True, "times": times}
                await self._show_client_reminder_step(update, context, 2)
            elif state == "clientrem_water_value":
                draft = context.user_data["nutrition_clientrem_draft"]
                if draft["water"]["mode"] == "times":
                    draft["water"].update(times=self._parse_times(text, limit=12))
                else:
                    interval = self._positive_number(text, "Интервал", maximum=720)
                    if int(interval) != interval or interval < 60:
                        raise ValueError("Интервал должен быть от 60 до 720 минут")
                    draft["water"].update(interval_minutes=int(interval))
                await self._show_client_reminder_step(update, context, 4)
            elif state == "clientrem_water_range":
                start, end = self._parse_range(text)
                context.user_data["nutrition_clientrem_draft"]["water"].update(start_local=start, end_local=end)
                await self._show_client_reminder_step(update, context, 4)
            elif state == "clientrem_quiet":
                start, end = self._parse_range(text)
                context.user_data["nutrition_clientrem_draft"]["quiet_hours"] = {"start": start, "end": end}
                await self._show_client_reminder_step(update, context, 5)
            elif state == "trainerrem_time":
                context.user_data["nutrition_trainerrem_draft"]["daily_digest"]["time"] = self._parse_times(text, limit=1)[0]
                await self._show_trainer_reminder_step(update, context, 1)
            elif state == "trainerrem_days":
                days = self._positive_number(text, "Дни без записей", maximum=7)
                if int(days) != days:
                    raise ValueError("Дни без записей: введите целое число от 1 до 7")
                context.user_data["nutrition_trainerrem_draft"]["daily_digest"]["inactivity_days"] = int(days)
                await self._show_trainer_reminder_step(update, context, 2)
            elif state == "trainerrem_percent":
                percent = int(self._nonnegative_number(text, "Порог", maximum=500))
                context.user_data["nutrition_trainerrem_draft"]["daily_digest"]["over_plan_percent"] = percent
                await self._show_trainer_reminder_step(update, context, 3)
            elif state == "mealctx_date_other":
                try:
                    chosen = date.fromisoformat(text)
                except ValueError as exc:
                    raise ValueError("Введите дату в формате ГГГГ-ММ-ДД") from exc
                context.user_data["nutrition_mealctx_draft"]["local_date"] = chosen.isoformat()
                await self._show_meal_context_step(update, context, 2)
            elif state == "mealctx_time":
                try:
                    parsed_time = datetime.strptime(text, "%H:%M").time()
                except ValueError as exc:
                    raise ValueError("Введите время в формате ЧЧ:ММ, например 13:30") from exc
                context.user_data["nutrition_mealctx_draft"]["local_time"] = parsed_time.strftime("%H:%M")
                await self._show_meal_context_step(update, context, 3)
            elif state == "mealctx_note":
                if len(text) > 1000:
                    raise ValueError("Заметка должна быть короче 1000 символов")
                context.user_data["nutrition_mealctx_draft"]["note"] = text
                await self._show_meal_context_step(update, context, 4)
            elif state == "reference_search":
                if self.reference is None:
                    raise ValueError("Справочник временно недоступен")
                context.user_data.pop("nutrition_reference_candidates", None)
                context.user_data.pop("nutrition_reference_screen", None)
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
        except RuntimeError:
            await update.message.reply_text(
                "⚠️ Данные уже изменились. Откройте текущий экран заново."
            )
            return True

    @staticmethod
    def _capture_help_return(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сохраняет весь шаг дневника до входа в справку."""
        if "nutrition_help_return" not in context.user_data:
            context.user_data["nutrition_help_return"] = {
                key: copy.deepcopy(value)
                for key, value in context.user_data.items()
                if key.startswith("nutrition_") and not key.startswith("nutrition_help_")
            }
        context.user_data["nutrition_state"] = "nutrition_help_question"

    def _help_context(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> NutritionHelpContext:
        saved = context.user_data.get("nutrition_help_return") or context.user_data
        state = str(saved.get("nutrition_state") or "menu")
        screens = {
            "menu": "Меню дневника",
            "reference_search": "Поиск продукта в справочнике",
            "reference_grams": "Масса продукта из справочника",
            "reference_reweight": "Изменение массы продукта",
            "wait_photo": "Добавление фото",
            "wait_manual": "Описание приема пищи с помощью ИИ",
            "manual_only": "Ручной ввод КБЖУ",
            "clarify_ai": "Уточнение черновика ИИ",
            "draft": "Черновик приема пищи",
            "edit_draft": "Исправление черновика",
            "wait_water": "Добавление воды",
            "wait_link_code": "Привязка к тренеру",
            "wait_timezone": "Часовой пояс",
            "norms_date": "Дата начала норм",
            "norms_date_other": "Ручной ввод даты норм",
            "norms_value": "Значения дневных норм",
            "norms_preview": "Проверка дневных норм",
            "trainer_plan_upload": "Загрузка плана XLSX",
            "trainer_plan_preview": "Проверка плана XLSX",
            "trainer_edit_items": "Исправление приема тренером",
            "trainer_comment_text": "Комментарий тренера",
            "manual_item": "Пошаговый ручной ввод продукта",
            "manual_preview": "Проверка продукта",
            "profile_value": "Изменение профиля",
            "profile_preview": "Проверка профиля",
            "weight_value": "Вес, кг, например 82.5",
            "weight_date": "Дата измерения веса",
            "weight_date_other": "Дата измерения веса, ГГГГ-ММ-ДД",
            "weight_time": "Местное время измерения веса, ЧЧ:ММ",
            "weight_note": "Необязательная заметка о весе",
            "weight_preview": "Проверка записи веса",
            "weight_edit_value": "Исправленный вес, кг",
            "weight_history_date": "Дата истории веса, ГГГГ-ММ-ДД",
            "mealctx_type": "Тип приема пищи",
            "mealctx_date": "Дата приема пищи",
            "mealctx_date_other": "Дата приема пищи, ГГГГ-ММ-ДД",
            "mealctx_time": "Местное время приема, ЧЧ:ММ",
            "mealctx_note": "Необязательная заметка к приему",
            "mealctx_hunger": "Голод перед едой, от 1 до 10, необязательно",
            "mealctx_mood": "Настроение, необязательно",
            "mealctx_preview": "Проверка контекста приема",
            "clientrem_meal": "Напоминания о внесении приемов пищи",
            "clientrem_meal_times": "Местное время напоминаний о еде, ЧЧ:ММ",
            "clientrem_water": "Режим напоминаний о воде",
            "clientrem_water_value": "Время или интервал напоминаний о воде",
            "clientrem_water_range": "Границы дня для воды, ЧЧ:ММ-ЧЧ:ММ",
            "clientrem_quiet": "Тихие часы, ЧЧ:ММ-ЧЧ:ММ",
            "clientrem_preview": "Проверка расписания напоминаний",
            "trainerrem_time": "Местное время сводки тренера, ЧЧ:ММ",
            "trainerrem_days": "Порог отсутствия записей, 1-7 дней",
            "trainerrem_compare": "Сравнение подтвержденных калорий с нормой",
            "trainerrem_percent": "Порог превышения нормы, проценты",
            "trainerrem_preview": "Проверка настроек сводки тренера",
        }
        role = "trainer" if self.is_trainer(update.effective_user.id) else "client"
        screen = screens.get(state, "Дневник питания")
        if state == "reference_search" and saved.get("nutrition_reference_screen") == "candidates":
            screen = "Выбор продукта из результатов справочника"
        elif state == "norms_value":
            step = int(saved.get("nutrition_norms_step", 0))
            if 0 <= step < len(self._NORMS_FIELDS):
                _, label, unit = self._NORMS_FIELDS[step]
                screen = f"Нормы: {label}, {unit}"
        elif state == "manual_item":
            step = int(saved.get("nutrition_manual_step", 0))
            if 0 <= step < len(self._MANUAL_FIELDS):
                _, label, hint = self._MANUAL_FIELDS[step]
                screen = f"Ручной ввод: {label}, {hint}"
        elif state == "profile_value":
            step = int(saved.get("nutrition_profile_step", 0))
            if 0 <= step < len(self._PROFILE_FIELDS):
                _, label, hint = self._PROFILE_FIELDS[step]
                screen = f"Профиль: {label}, {hint}"
        return NutritionHelpContext(role=role, state=state, screen=screen)

    @staticmethod
    def _help_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Продолжить", callback_data="nutrition:help_resume")],
            [InlineKeyboardButton("🥗 В меню дневника", callback_data="nutrition:menu")],
        ])

    async def _enter_help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        self._capture_help_return(context)
        await self._reply(
            update,
            "❓ <b>Помощь по дневнику</b>\n\n"
            "Задайте вопрос о работе бота. Можно написать несколько вопросов "
            "отдельными пунктами. Предыдущий шаг сохранен.",
            parse_mode="HTML",
            reply_markup=self._help_keyboard(),
        )

    async def _answer_help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        question: str,
    ) -> None:
        self._capture_help_return(context)
        help_context = self._help_context(update, context)
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(self.help_service.answer, question, help_context),
                timeout=float(getattr(self.help_service, "timeout_seconds", 12)) + 1,
            )
        except ValueError:
            raise
        except Exception:
            answer = self.help_service.fallback_answer(question, help_context)
        context.user_data["nutrition_state"] = "nutrition_help_question"
        await update.message.reply_text(
            html.escape(answer),
            parse_mode="HTML",
            reply_markup=self._help_keyboard(),
        )

    async def _resume_help(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        saved = context.user_data.get("nutrition_help_return")
        if not isinstance(saved, dict):
            state = context.user_data.get("nutrition_state")
            if state and state != "nutrition_help_question":
                await self._reply(
                    update,
                    "Эта кнопка относится к прежнему экрану. Текущий шаг сохранен:",
                )
                await self._render_resumed_step(update, context)
            else:
                await self._send_menu(update, context, edit=True)
            return

        try:
            client_id = int(saved.get("nutrition_trainer_client_id", 0) or 0)
            meal_id = int(saved.get("nutrition_trainer_meal_id", 0) or 0)
            if client_id:
                self._require_trainer(update.effective_user.id)
                self.store.get_client_day(
                    update.effective_user.id,
                    client_id,
                    self._today_for_client(update.effective_user.id, client_id),
                )
            if meal_id:
                self._require_trainer(update.effective_user.id)
                self.store.get_client_meal(update.effective_user.id, meal_id)
            draft_meal_id = int(
                saved.get("nutrition_draft_meal_id", 0)
                or saved.get("nutrition_edit_meal_id", 0)
                or saved.get("nutrition_reference_meal_id", 0)
                or saved.get("nutrition_mealctx_meal_id", 0)
                or 0
            )
            if draft_meal_id:
                self.store.get_owned_draft(
                    client_telegram_id=update.effective_user.id,
                    meal_id=draft_meal_id,
                )
            weight_edit = saved.get("nutrition_weight_edit")
            if isinstance(weight_edit, dict) and weight_edit.get("id"):
                self._find_weight_entry(
                    update.effective_user.id,
                    date.fromisoformat(weight_edit["local_date"]),
                    int(weight_edit["id"]),
                    int(weight_edit["version"]),
                )
        except (PermissionError, ValueError):
            self._clear(context)
            context.user_data["nutrition_active"] = True
            raise PermissionError(
                "Предыдущий шаг больше недоступен. Откройте клиента и начните действие заново."
            )

        self._clear(context)
        context.user_data.update(copy.deepcopy(saved))
        await self._render_resumed_step(update, context)

    async def _render_resumed_step(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Повторно показывает восстановленный шаг с его настоящими кнопками."""
        state = context.user_data.get("nutrition_state")
        if state == "norms_date":
            await self._show_norms_date(update, context)
        elif state in {"norms_value", "norms_preview"}:
            await self._show_norms_step(
                update,
                context,
                int(context.user_data.get("nutrition_norms_step", 0)),
            )
        elif state == "norms_date_other":
            client_id = int(context.user_data["nutrition_trainer_client_id"])
            nonce = context.user_data["nutrition_norms_nonce"]
            await self._reply(
                update,
                "Введите дату начала действия в формате ГГГГ-ММ-ДД:",
                reply_markup=self._cancel_keyboard(
                    f"nutrition:norms_date_back:{client_id}:{nonce}"
                ),
            )
        elif state == "trainer_plan_upload":
            client_id = int(context.user_data["nutrition_trainer_client_id"])
            await self._reply(
                update,
                "Пришлите файл .xlsx до 2 МБ. Каждая строка задает дневные нормы "
                "с указанной даты до следующей строки плана.",
                reply_markup=self._cancel_keyboard(f"nutrition:trainer_day:{client_id}"),
            )
        elif state == "trainer_plan_preview":
            await self._show_pending_plan(update, context)
        elif state == "trainer_comment_text":
            meal_id = int(context.user_data["nutrition_trainer_meal_id"])
            meal = self.store.get_client_meal(update.effective_user.id, meal_id)
            await self._reply(
                update,
                self._meal_context(meal) + "\n\nНапишите комментарий обычным текстом:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                    [InlineKeyboardButton(
                        "⬅️ К выбору записи",
                        callback_data=f"nutrition:trainer_comment:{meal['client_user_id']}",
                    )],
                    [InlineKeyboardButton(
                        "Отмена", callback_data=f"nutrition:trainer_day:{meal['client_user_id']}"
                    )],
                ]),
            )
        elif state == "trainer_edit_items":
            meal_id = int(context.user_data["nutrition_trainer_meal_id"])
            meal = self.store.get_client_meal(update.effective_user.id, meal_id)
            await self._reply(
                update,
                self._meal_context(meal) + "\n\nПришлите исправленные блюда по одному на строку:\n"
                "Название; масса в г или описание порции; ккал; белки; жиры; углеводы",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                    [InlineKeyboardButton(
                        "⬅️ К выбору записи",
                        callback_data=f"nutrition:trainer_edit:{meal['client_user_id']}",
                    )],
                    [InlineKeyboardButton(
                        "Отмена", callback_data=f"nutrition:trainer_day:{meal['client_user_id']}"
                    )],
                ]),
            )
        elif state == "reference_search" and context.user_data.get("nutrition_reference_screen") == "candidates":
            if self.reference is None:
                raise ValueError("Справочник временно недоступен")
            candidates = [
                self.reference.get(fdc_id)
                for fdc_id in context.user_data.get("nutrition_reference_candidates", [])
            ]
            await self._show_reference_candidates(
                update,
                context,
                candidates,
                query=context.user_data.get("nutrition_reference_query", "продукт"),
            )
        elif state == "reference_search":
            await self._reply(
                update,
                "Введите продукт и способ приготовления, например «рис вареный» или "
                "«куриная грудка запеченная»:",
                reply_markup=self._cancel_keyboard(self._reference_back_callback(context)),
            )
        elif state == "reference_grams":
            if self.reference is None:
                raise ValueError("Справочник временно недоступен")
            record = self.reference.get(context.user_data.get("nutrition_reference_fdc_id"))
            preparation = f" ({record.preparation})" if record.preparation else ""
            await self._reply(
                update,
                f"Вы выбрали: <b>{html.escape(record.display_name)}</b>"
                f"{html.escape(preparation)}.\nВведите массу в граммах:",
                parse_mode="HTML",
                reply_markup=self._cancel_keyboard("nutrition:ref_back_candidates"),
            )
        elif state == "reference_reweight":
            meal_id = int(context.user_data["nutrition_reference_meal_id"])
            await self._reply(
                update,
                "Введите новую массу продукта в граммах:",
                reply_markup=self._cancel_keyboard(f"nutrition:refine:{meal_id}"),
            )
        elif state == "reference_review":
            await self._show_reference_builder(update, context)
        elif state == "wait_photo":
            if context.user_data.get("nutrition_pending_photo"):
                await self._reply(
                    update,
                    "Для разбора фото оно будет передано искусственному интеллекту. Анализ создает приблизительный "
                    "черновик. Согласие сохранится для следующих фото, его можно отозвать в меню.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Согласен, анализировать", callback_data="nutrition:consent_yes")],
                        [InlineKeyboardButton("Без ИИ, введу вручную", callback_data="nutrition:consent_no")],
                        [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:add_meal")],
                        [InlineKeyboardButton("Отмена", callback_data="nutrition:menu")],
                    ]),
                )
            else:
                await self._reply(
                    update,
                    "📸 Пришлите одно фото еды. Сначала будет создан черновик для проверки.",
                    reply_markup=self._cancel_keyboard("nutrition:add_meal"),
                )
        elif state == "wait_manual":
            await self._reply(
                update,
                "Опишите еду и размер порции. Описание будет передано ИИ для "
                "приблизительного черновика:",
                reply_markup=self._cancel_keyboard("nutrition:add_meal"),
            )
        elif state == "manual_only":
            await self._reply(
                update,
                "Ручной ввод без ИИ.\n\n" + self._manual_prompt(),
                reply_markup=self._cancel_keyboard("nutrition:add_meal"),
            )
        elif state == "clarify_ai":
            await self._reply(
                update,
                "Уточните состав блюда или размер порции. Исходное описание сохранено:",
                reply_markup=self._cancel_keyboard("nutrition:add_meal"),
            )
        elif state == "draft":
            meal = self.store.get_owned_draft(
                client_telegram_id=update.effective_user.id,
                meal_id=int(context.user_data["nutrition_draft_meal_id"]),
            )
            await self._show_draft(update, context, meal)
        elif state == "edit_draft":
            meal_id = int(context.user_data["nutrition_edit_meal_id"])
            await self._reply(
                update,
                "Пришлите исправленный список, каждое блюдо с новой строки:\n"
                "Название; масса в г или описание порции; ккал; белки; жиры; углеводы",
                reply_markup=self._cancel_keyboard(f"nutrition:draft:{meal_id}"),
            )
        elif state == "wait_water":
            await self._reply(
                update,
                "Введите объем воды целым числом в миллилитрах:",
                reply_markup=self._cancel_keyboard("nutrition:water"),
            )
        elif state == "wait_link_code":
            await self._reply(
                update,
                "Введите код, который прислал ваш тренер:",
                reply_markup=self._cancel_keyboard("nutrition:menu"),
            )
        elif state == "wait_timezone":
            await self._reply(
                update,
                "Введите часовой пояс в формате IANA, например Europe/Moscow или Asia/Tokyo:",
                reply_markup=self._cancel_keyboard("nutrition:menu"),
            )
        elif state in {"manual_item", "manual_preview"}:
            await self._show_manual_step(
                update, context, int(context.user_data.get("nutrition_manual_step", 0))
            )
        elif state in {"profile_value", "profile_preview"}:
            await self._show_profile_step(
                update, context, int(context.user_data.get("nutrition_profile_step", 0))
            )
        elif state == "weight_value":
            await self._show_weight_value(update, context)
        elif state == "weight_date":
            await self._show_weight_date(update, context)
        elif state == "weight_date_other":
            nonce = context.user_data["nutrition_weight_nonce"]
            await self._reply(
                update, "Введите дату измерения ГГГГ-ММ-ДД:",
                reply_markup=self._weight_footer(nonce),
            )
        elif state == "weight_time":
            await self._show_weight_time(update, context)
        elif state == "weight_note":
            nonce = context.user_data["nutrition_weight_nonce"]
            await self._reply(
                update, "Добавьте необязательную заметку или нажмите «Пропустить»:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "Пропустить",
                        callback_data=f"nutrition:weight_note_skip:{nonce}",
                    )],
                    [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data=f"nutrition:weight_back:{nonce}")],
                    [InlineKeyboardButton("Отмена", callback_data="nutrition:weight")],
                ]),
            )
        elif state == "weight_preview":
            await self._show_weight_preview(update, context)
        elif state == "weight_edit_value":
            row = context.user_data.get("nutrition_weight_edit") or {}
            await self._reply(
                update, f"Текущий вес {row['weight_kg']:g} кг. Введите исправленное значение:",
                reply_markup=self._cancel_keyboard("nutrition:weight_day"),
            )
        elif state == "weight_history_date":
            await self._reply(
                update, "Введите дату записей веса в формате ГГГГ-ММ-ДД:",
                reply_markup=self._cancel_keyboard("nutrition:weight"),
            )
        elif state == "mealctx_date_other":
            nonce = context.user_data["nutrition_mealctx_nonce"]
            await self._reply(
                update, "Введите дату ГГГГ-ММ-ДД:",
                reply_markup=InlineKeyboardMarkup(self._mealctx_footer(nonce)),
            )
        elif isinstance(state, str) and state.startswith("mealctx_"):
            await self._show_meal_context_step(
                update, context, int(context.user_data.get("nutrition_mealctx_step", 0))
            )
        elif isinstance(state, str) and state.startswith("clientrem_"):
            if state == "clientrem_menu":
                await self._show_client_reminders(update, context)
            else:
                await self._show_client_reminder_step(update, context, int(context.user_data.get("nutrition_clientrem_step", 0)))
        elif isinstance(state, str) and state.startswith("trainerrem_"):
            self._require_trainer(update.effective_user.id)
            if state == "trainerrem_menu":
                await self._show_trainer_reminders(update, context)
            else:
                await self._show_trainer_reminder_step(update, context, int(context.user_data.get("nutrition_trainerrem_step", 0)))
        else:
            await self._send_menu(update, context, edit=True)

    async def _analyze_photo(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        file_id: str,
    ) -> None:
        if not self.ai.available:
            await self._reply(
                update,
                "Анализ фото с помощью ИИ пока не настроен. Фото не сохранено локально. "
                "Перейдем к пошаговому ручному вводу.",
            )
            await self._start_manual_wizard(update, context)
            return
        try:
            telegram_file = await context.bot.get_file(file_id)
            image = bytes(await telegram_file.download_as_bytearray())
            result = await asyncio.wait_for(
                asyncio.to_thread(self.ai.analyze_photo, image, "image/jpeg"),
                timeout=self.ai.timeout_seconds + 3,
            )
        except Exception:
            await self._reply(
                update,
                "Не удалось безопасно получить или разобрать фото. Фото не учтено. "
                "Перейдем к пошаговому ручному вводу.",
            )
            await self._start_manual_wizard(update, context)
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
                "Анализ текста с помощью ИИ не настроен. Используйте строки: "
                "Название; масса или описание порции; ккал; белки; жиры; углеводы"
            )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.ai.analyze_text, text),
                timeout=self.ai.timeout_seconds + 3,
            )
        except (NutritionAIUnavailable, asyncio.TimeoutError) as exc:
            raise ValueError(
                "ИИ не ответил. Используйте структурный формат с КБЖУ."
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
        context.user_data["nutrition_state"] = "reference_search"
        context.user_data["nutrition_reference_screen"] = "candidates"
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
        buttons.append([InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")])
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
        context.user_data.pop("nutrition_reference_screen", None)
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
                [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
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
            [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_callback)],
            [InlineKeyboardButton("Отмена", callback_data="nutrition:menu")],
        ])

    @staticmethod
    def _require_self_flow(
        context: ContextTypes.DEFAULT_TYPE,
        kind: str,
        nonce: str,
        expected_state: str | None = None,
    ) -> None:
        if (
            context.user_data.get(f"nutrition_{kind}_nonce") != nonce
            or (expected_state is not None and context.user_data.get("nutrition_state") != expected_state)
        ):
            raise PermissionError("Этот экран уже устарел. Начните действие заново.")

    @staticmethod
    def _positive_number(text: str, label: str, *, maximum: float) -> float:
        try:
            value = float(text.replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{label}: введите число") from exc
        if not math.isfinite(value) or value <= 0 or value > maximum:
            raise ValueError(f"{label}: значение должно быть больше 0 и не больше {maximum:g}")
        return value

    @staticmethod
    def _nonnegative_number(text: str, label: str, *, maximum: float) -> float:
        try:
            value = float(text.replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{label}: введите число") from exc
        if not math.isfinite(value) or value < 0 or value > maximum:
            raise ValueError(f"{label}: значение должно быть от 0 до {maximum:g}")
        return value

    async def _start_manual_wizard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._clear_manual_flow(context)
        context.user_data["nutrition_manual_nonce"] = self._new_flow_nonce()
        context.user_data["nutrition_manual_item"] = {}
        await self._show_manual_step(update, context, 0)

    async def _show_manual_step(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, step: int
    ) -> None:
        nonce = str(context.user_data.get("nutrition_manual_nonce", ""))
        self._require_self_flow(context, "manual", nonce)
        draft = context.user_data.get("nutrition_manual_item") or {}
        if step >= len(self._MANUAL_FIELDS):
            context.user_data["nutrition_state"] = "manual_preview"
            context.user_data["nutrition_manual_step"] = len(self._MANUAL_FIELDS)
            await self._reply(
                update,
                "Проверьте продукт:\n"
                f"{html.escape(str(draft['name']))}, {draft['weight_g']:g} г, "
                f"{draft['calories']:g} ккал, Б {draft['protein_g']:g} г, "
                f"Ж {draft['fat_g']:g} г, У {draft['carbs_g']:g} г.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ Создать черновик",
                        callback_data=f"nutrition:manual_confirm:{nonce}",
                    )],
                    [InlineKeyboardButton(
                        "⬅️ Назад", callback_data=f"nutrition:manual_back:{nonce}"
                    )],
                    [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                    [InlineKeyboardButton("Отмена", callback_data="nutrition:add_meal")],
                ]),
            )
            return
        context.user_data["nutrition_state"] = "manual_item"
        context.user_data["nutrition_manual_step"] = step
        key, label, hint = self._MANUAL_FIELDS[step]
        previous = draft.get(key)
        previous_text = f" Ранее введено: {previous}." if previous is not None else ""
        buttons = []
        if key in draft:
            buttons.append([InlineKeyboardButton(
                "Оставить введенное",
                callback_data=f"nutrition:manual_keep:{nonce}",
            )])
        buttons.extend([
            [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
            [InlineKeyboardButton(
                "⬅️ Назад", callback_data=f"nutrition:manual_back:{nonce}"
            )],
            [InlineKeyboardButton("Отмена", callback_data="nutrition:add_meal")],
        ])
        await self._reply(
            update,
            f"Шаг {step + 1} из {len(self._MANUAL_FIELDS)}. {label}: {hint}.{previous_text}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _accept_manual_value(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        step = int(context.user_data.get("nutrition_manual_step", 0))
        key, label, _ = self._MANUAL_FIELDS[step]
        if key == "name":
            value: Any = text.strip()
            if not value or len(value) > 200:
                raise ValueError("Название продукта должно содержать от 1 до 200 символов")
        else:
            maximum = 100_000 if key in {"weight_g", "calories"} else 10_000
            value = (
                self._positive_number(text, label, maximum=maximum)
                if key == "weight_g"
                else self._nonnegative_number(text, label, maximum=maximum)
            )
        context.user_data["nutrition_manual_item"][key] = value
        await self._show_manual_step(update, context, step + 1)

    async def _confirm_manual_item(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        item = dict(context.user_data.get("nutrition_manual_item") or {})
        meal = self.store.create_meal_draft(
            client_telegram_id=update.effective_user.id,
            source="manual",
            eaten_at=datetime.now(timezone.utc),
            meal_type=self._meal_type(update.effective_user.id),
            items=[{
                **item,
                "portion_text": "",
                "approximate": False,
            }],
        )
        self._clear_manual_flow(context)
        await self._show_draft(update, context, meal)

    @staticmethod
    def _clear_manual_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
        for key in ("nutrition_manual_nonce", "nutrition_manual_item", "nutrition_manual_step"):
            context.user_data.pop(key, None)
        if context.user_data.get("nutrition_state") in {"manual_item", "manual_preview"}:
            context.user_data.pop("nutrition_state", None)

    async def _show_profile(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        profile = self.store.get_profile(telegram_id=update.effective_user.id)
        height = "не указан" if profile["height_cm"] is None else f"{profile['height_cm']:g} см"
        goal = profile["goal"] or "не указана"
        await self._reply(
            update,
            "👤 <b>Профиль</b>\n\n"
            f"Имя: {html.escape(profile['display_name'])}\n"
            f"Рост: {height}\n"
            f"Цель: {html.escape(goal)}\n"
            f"Часовой пояс: {html.escape(profile['timezone'])}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить профиль", callback_data="nutrition:profile_edit")],
                [InlineKeyboardButton("❓ Помощь", callback_data="nutrition:help")],
                [InlineKeyboardButton("⬅️ В дневник", callback_data="nutrition:menu")],
            ]),
        )

    async def _start_profile_wizard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._clear_profile_flow(context)
        profile = self.store.get_profile(telegram_id=update.effective_user.id)
        context.user_data["nutrition_profile_nonce"] = self._new_flow_nonce()
        context.user_data["nutrition_profile_version"] = profile["version"]
        context.user_data["nutrition_profile_prior"] = profile
        context.user_data["nutrition_profile_draft"] = {}
        await self._show_profile_step(update, context, 0)

    async def _show_profile_step(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, step: int
    ) -> None:
        nonce = str(context.user_data.get("nutrition_profile_nonce", ""))
        self._require_self_flow(context, "profile", nonce)
        draft = context.user_data.get("nutrition_profile_draft") or {}
        prior = context.user_data.get("nutrition_profile_prior") or {}
        if step >= len(self._PROFILE_FIELDS):
            context.user_data["nutrition_state"] = "profile_preview"
            context.user_data["nutrition_profile_step"] = len(self._PROFILE_FIELDS)
            height_value = draft["height_cm"]
            height_text = "не указан" if height_value is None else f"{height_value:g} см"
            await self._reply(
                update,
                "Проверьте профиль:\n"
                f"Имя: {html.escape(str(draft['display_name']))}\n"
                f"Рост: {height_text}\n"
                f"Цель: {html.escape(draft['goal'] or 'не указана')}\n"
                f"Часовой пояс: {html.escape(draft['timezone'])}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Сохранить", callback_data=f"nutrition:profile_confirm:{nonce}")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data=f"nutrition:profile_back:{nonce}")],
                    [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                    [InlineKeyboardButton("Отмена", callback_data="nutrition:profile")],
                ]),
            )
            return
        context.user_data["nutrition_state"] = "profile_value"
        context.user_data["nutrition_profile_step"] = step
        key, label, hint = self._PROFILE_FIELDS[step]
        current = prior.get(key)
        shown = "не указано" if current in (None, "") else str(current)
        buttons = [[InlineKeyboardButton(
            f"Оставить: {shown}"[:60], callback_data=f"nutrition:profile_choice:{nonce}:keep"
        )]]
        if key in draft:
            entered = draft[key]
            entered_text = "не указано" if entered in (None, "") else str(entered)
            buttons.insert(0, [InlineKeyboardButton(
                f"Оставить введенное: {entered_text}"[:60],
                callback_data=f"nutrition:profile_choice:{nonce}:keep_draft",
            )])
        if key in {"height_cm", "goal"}:
            buttons.append([InlineKeyboardButton(
                "Не указывать", callback_data=f"nutrition:profile_choice:{nonce}:none"
            )])
        buttons.extend([
            [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"nutrition:profile_back:{nonce}")],
            [InlineKeyboardButton("Отмена", callback_data="nutrition:profile")],
        ])
        await self._reply(update, f"{label}: {hint}.", reply_markup=InlineKeyboardMarkup(buttons))

    async def _accept_profile_value(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        step = int(context.user_data.get("nutrition_profile_step", 0))
        key, _, _ = self._PROFILE_FIELDS[step]
        if key == "display_name":
            value: Any = text.strip()
            if not value or len(value) > 200:
                raise ValueError("Имя должно содержать от 1 до 200 символов")
        elif key == "height_cm":
            value = self._positive_number(text, "Рост", maximum=300)
        elif key == "goal":
            value = text.strip()
            if len(value) > 1000:
                raise ValueError("Описание цели должно быть короче 1000 символов")
        else:
            try:
                ZoneInfo(text.strip())
            except Exception as exc:
                raise ValueError("Неизвестный часовой пояс IANA") from exc
            value = text.strip()
        context.user_data["nutrition_profile_draft"][key] = value
        await self._show_profile_step(update, context, step + 1)

    async def _apply_profile_choice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str
    ) -> None:
        step = int(context.user_data.get("nutrition_profile_step", 0))
        key, _, _ = self._PROFILE_FIELDS[step]
        if choice == "keep_draft" and key in context.user_data.get("nutrition_profile_draft", {}):
            value = context.user_data["nutrition_profile_draft"][key]
        elif choice == "keep":
            value = (context.user_data.get("nutrition_profile_prior") or {}).get(key)
        elif choice == "none" and key in {"height_cm", "goal"}:
            value = None if key == "height_cm" else ""
        else:
            raise ValueError("Неизвестный вариант профиля")
        context.user_data["nutrition_profile_draft"][key] = value
        await self._show_profile_step(update, context, step + 1)

    async def _confirm_profile(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        result = self.store.update_profile(
            telegram_id=update.effective_user.id,
            updates=dict(context.user_data["nutrition_profile_draft"]),
            expected_version=int(context.user_data["nutrition_profile_version"]),
        )
        self._clear_profile_flow(context)
        await self._reply(
            update,
            f"✅ Профиль сохранен. Часовой пояс: {html.escape(result['timezone'])}.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 Открыть профиль", callback_data="nutrition:profile")]
            ]),
        )

    @staticmethod
    def _clear_profile_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
        for key in (
            "nutrition_profile_nonce", "nutrition_profile_version", "nutrition_profile_prior",
            "nutrition_profile_draft", "nutrition_profile_step",
        ):
            context.user_data.pop(key, None)
        if context.user_data.get("nutrition_state") in {"profile_value", "profile_preview"}:
            context.user_data.pop("nutrition_state", None)

    async def _show_weight_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        today = self._today(update.effective_user.id)
        day = self.store.get_own_day(
            client_telegram_id=update.effective_user.id, local_date=today
        )
        current = "нет записи" if day.get("weight_kg") is None else f"{day['weight_kg']:g} кг"
        await self._reply(
            update,
            f"⚖️ <b>Вес</b>\n\nПоследняя запись сегодня: {current}.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Записать вес", callback_data="nutrition:weight_add")],
                [InlineKeyboardButton("📈 История", callback_data="nutrition:weight_history")],
                [InlineKeyboardButton("📅 Исправить по дате", callback_data="nutrition:weight_day")],
                [InlineKeyboardButton("❓ Помощь", callback_data="nutrition:help")],
                [InlineKeyboardButton("⬅️ В дневник", callback_data="nutrition:menu")],
            ]),
        )

    async def _start_weight_wizard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._clear_weight_flow(context)
        context.user_data["nutrition_weight_nonce"] = self._new_flow_nonce()
        context.user_data["nutrition_weight_draft"] = {}
        await self._show_weight_value(update, context)

    async def _show_weight_value(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        nonce = str(context.user_data.get("nutrition_weight_nonce", ""))
        self._require_self_flow(context, "weight", nonce)
        draft = context.user_data.get("nutrition_weight_draft") or {}
        context.user_data["nutrition_state"] = "weight_value"
        previous = draft.get("weight_kg")
        suffix = "" if previous is None else f" Ранее введено: {previous:g} кг."
        rows = []
        if previous is not None:
            rows.append([InlineKeyboardButton(
                "Оставить введенное",
                callback_data=f"nutrition:weight_keep:{nonce}",
            )])
        rows.extend(self._weight_footer_rows(nonce, include_back=False))
        await self._reply(
            update,
            "Введите вес в килограммах, например 82.5." + suffix,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _show_weight_date(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        nonce = str(context.user_data.get("nutrition_weight_nonce", ""))
        self._require_self_flow(context, "weight", nonce)
        context.user_data["nutrition_state"] = "weight_date"
        draft = context.user_data.get("nutrition_weight_draft") or {}
        rows = []
        if draft.get("local_date"):
            rows.append([InlineKeyboardButton(
                f"Оставить: {draft['local_date']}",
                callback_data=f"nutrition:weight_date:{nonce}:keep_date",
            )])
        rows.extend([
            [InlineKeyboardButton("Сегодня", callback_data=f"nutrition:weight_date:{nonce}:today")],
            [InlineKeyboardButton("Вчера", callback_data=f"nutrition:weight_date:{nonce}:yesterday")],
            [InlineKeyboardButton("Другая дата", callback_data=f"nutrition:weight_date:{nonce}:other")],
        ])
        rows.extend(self._weight_footer_rows(nonce))
        await self._reply(
            update, "Выберите местную дату измерения:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _show_weight_time(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        nonce = str(context.user_data.get("nutrition_weight_nonce", ""))
        self._require_self_flow(context, "weight", nonce)
        context.user_data["nutrition_state"] = "weight_time"
        draft = context.user_data.get("nutrition_weight_draft") or {}
        previous = draft.get("local_time")
        rows = []
        if previous:
            rows.append([InlineKeyboardButton(
                f"Оставить: {previous}",
                callback_data=f"nutrition:weight_time_keep:{nonce}",
            )])
        rows.extend(self._weight_footer_rows(nonce))
        await self._reply(
            update, "Введите местное время измерения в формате ЧЧ:ММ, например 07:30:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _accept_weight_time(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        try:
            parsed = datetime.strptime(text.strip(), "%H:%M").time()
        except ValueError as exc:
            raise ValueError("Введите местное время в формате ЧЧ:ММ, например 07:30") from exc
        draft = context.user_data["nutrition_weight_draft"]
        chosen = date.fromisoformat(draft["local_date"])
        user = self.store.get_user_by_telegram_id(update.effective_user.id)
        local = datetime.combine(chosen, parsed, tzinfo=ZoneInfo(user["timezone"]))
        draft["local_time"] = parsed.strftime("%H:%M")
        draft["measured_at"] = local.isoformat()
        context.user_data["nutrition_state"] = "weight_note"
        nonce = context.user_data["nutrition_weight_nonce"]
        await self._reply(
            update, "Добавьте необязательную заметку или нажмите «Пропустить»:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Пропустить", callback_data=f"nutrition:weight_note_skip:{nonce}")],
            ] + self._weight_footer_rows(nonce)),
        )

    async def _back_weight_wizard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        state = context.user_data.get("nutrition_state")
        if state == "weight_date":
            await self._show_weight_value(update, context)
        elif state == "weight_date_other":
            await self._show_weight_date(update, context)
        elif state == "weight_time":
            await self._show_weight_date(update, context)
        elif state == "weight_note":
            await self._show_weight_time(update, context)
        elif state == "weight_preview":
            context.user_data["nutrition_state"] = "weight_note"
            await self._render_resumed_step(update, context)
        else:
            raise PermissionError("Эта кнопка относится к другому шагу")

    @staticmethod
    def _weight_footer_rows(
        nonce: str, *, include_back: bool = True
    ) -> list[list[InlineKeyboardButton]]:
        rows = [[InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")]]
        if include_back:
            rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"nutrition:weight_back:{nonce}")])
        rows.append([InlineKeyboardButton("Отмена", callback_data="nutrition:weight")])
        return rows

    def _weight_footer(self, nonce: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(self._weight_footer_rows(nonce))

    async def _show_weight_preview(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        draft = context.user_data.get("nutrition_weight_draft") or {}
        nonce = str(context.user_data.get("nutrition_weight_nonce", ""))
        self._require_self_flow(context, "weight", nonce)
        context.user_data["nutrition_state"] = "weight_preview"
        note = draft.get("note") or "без заметки"
        await self._reply(
            update,
            f"Проверьте запись веса: {draft['weight_kg']:g} кг, "
            f"{draft['local_date']} {draft['local_time']}, {html.escape(note)}.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Сохранить", callback_data=f"nutrition:weight_confirm:{nonce}")],
                [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                [InlineKeyboardButton("⬅️ Назад", callback_data=f"nutrition:weight_back:{nonce}")],
                [InlineKeyboardButton("Отмена", callback_data="nutrition:weight")],
            ]),
        )

    async def _confirm_weight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        draft = dict(context.user_data.get("nutrition_weight_draft") or {})
        nonce = str(context.user_data.get("nutrition_weight_nonce", ""))
        row = self.store.add_weight(
            client_telegram_id=update.effective_user.id,
            weight_kg=draft["weight_kg"],
            measured_at=draft["measured_at"],
            note=draft.get("note", ""),
            idempotency_key=f"weight:{update.effective_user.id}:{nonce}",
        )
        self._clear_weight_flow(context)
        await self._reply(
            update,
            f"✅ Вес {row['weight_kg']:g} кг сохранен.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚖️ К весу", callback_data="nutrition:weight")]
            ]),
        )

    async def _show_weight_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        end = self._today(update.effective_user.id)
        start = end - timedelta(days=13)
        summary = self.store.get_own_summary(
            client_telegram_id=update.effective_user.id,
            date_from=start,
            date_to=end,
        )
        rows = [
            f"• {row['date']}: {row['weight_kg']:g} кг"
            for row in summary["series"] if row.get("weight_kg") is not None
        ]
        text = "⚖️ <b>Вес за 14 дней</b>\n\n" + ("\n".join(rows) if rows else "Записей нет.")
        await self._reply(
            update, text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Открыть дату", callback_data="nutrition:weight_day")],
                [InlineKeyboardButton("⬅️ К весу", callback_data="nutrition:weight")],
            ]),
        )

    async def _show_weight_day(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, chosen: date
    ) -> None:
        day = self.store.get_own_day(
            client_telegram_id=update.effective_user.id, local_date=chosen
        )
        entries = day.get("weight_entries") or []
        buttons = []
        lines = [f"⚖️ <b>Вес за {chosen.isoformat()}</b>"]
        for row in entries:
            lines.append(f"• {row['weight_kg']:g} кг")
            suffix = f"{chosen.isoformat()}:{row['id']}:{row['version']}"
            buttons.append([
                InlineKeyboardButton("✏️ Исправить", callback_data=f"nutrition:weight_edit:{suffix}"),
                InlineKeyboardButton("Отменить запись", callback_data=f"nutrition:weight_cancel:{suffix}"),
            ])
        if not entries:
            lines.append("Записей нет.")
        buttons.append([InlineKeyboardButton("⬅️ К весу", callback_data="nutrition:weight")])
        context.user_data.pop("nutrition_state", None)
        await self._reply(
            update, "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    def _find_weight_entry(
        self, telegram_id: int, chosen: date, weight_id: int, version: int
    ) -> dict[str, Any]:
        day = self.store.get_own_day(client_telegram_id=telegram_id, local_date=chosen)
        row = next(
            (item for item in day.get("weight_entries", []) if int(item["id"]) == weight_id),
            None,
        )
        if row is None or int(row["version"]) != version:
            raise PermissionError("Запись веса уже изменилась. Откройте дату заново.")
        return row

    async def _start_weight_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        date_value: str, weight_id: int, version: int,
    ) -> None:
        chosen = date.fromisoformat(date_value)
        row = self._find_weight_entry(update.effective_user.id, chosen, weight_id, version)
        context.user_data["nutrition_weight_edit"] = dict(row)
        context.user_data["nutrition_state"] = "weight_edit_value"
        await self._reply(
            update,
            f"Текущий вес {row['weight_kg']:g} кг. Введите исправленное значение:",
            reply_markup=self._cancel_keyboard("nutrition:weight_day"),
        )

    async def _apply_weight_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: float
    ) -> None:
        row = dict(context.user_data.get("nutrition_weight_edit") or {})
        updated = self.store.update_weight(
            client_telegram_id=update.effective_user.id,
            weight_id=int(row["id"]),
            weight_kg=value,
            measured_at=row["measured_at"],
            expected_version=int(row["version"]),
            note=row.get("note", ""),
        )
        context.user_data.pop("nutrition_weight_edit", None)
        context.user_data.pop("nutrition_state", None)
        await update.message.reply_text(
            f"✅ Вес исправлен: {updated['weight_kg']:g} кг.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚖️ К весу", callback_data="nutrition:weight")]
            ]),
        )

    async def _confirm_weight_cancel_screen(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        date_value: str, weight_id: int, version: int,
    ) -> None:
        chosen = date.fromisoformat(date_value)
        row = self._find_weight_entry(update.effective_user.id, chosen, weight_id, version)
        await self._reply(
            update,
            f"Отменить запись {row['weight_kg']:g} кг за {date_value}? Она останется в журнале действий.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "Да, отменить",
                    callback_data=f"nutrition:weight_cancel_confirm:{date_value}:{weight_id}:{version}",
                )],
                [InlineKeyboardButton("⬅️ Назад", callback_data="nutrition:weight_day")],
            ]),
        )

    @staticmethod
    def _clear_weight_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
        for key in ("nutrition_weight_nonce", "nutrition_weight_draft"):
            context.user_data.pop(key, None)
        if context.user_data.get("nutrition_state") in {
            "weight_value", "weight_date", "weight_date_other", "weight_time",
            "weight_note", "weight_preview",
        }:
            context.user_data.pop("nutrition_state", None)

    async def _start_meal_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, meal_id: int
    ) -> None:
        meal = self.store.get_own_meal(
            client_telegram_id=update.effective_user.id, meal_id=meal_id
        )
        user = self.store.get_user_by_telegram_id(update.effective_user.id)
        local = datetime.fromisoformat(meal["eaten_at"]).astimezone(ZoneInfo(user["timezone"]))
        self._clear_mealctx_flow(context)
        context.user_data["nutrition_mealctx_nonce"] = self._new_flow_nonce()
        context.user_data["nutrition_mealctx_meal_id"] = meal_id
        context.user_data["nutrition_mealctx_version"] = meal["version"]
        context.user_data["nutrition_mealctx_draft"] = {
            "meal_type": meal["meal_type"],
            "local_date": local.date().isoformat(),
            "local_time": local.strftime("%H:%M"),
            "note": meal.get("note", ""),
            "hunger_level": meal.get("hunger_level"),
            "mood": meal.get("mood"),
        }
        await self._show_meal_context_step(update, context, 0)

    async def _show_meal_context_step(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, step: int
    ) -> None:
        nonce = str(context.user_data.get("nutrition_mealctx_nonce", ""))
        self._require_self_flow(context, "mealctx", nonce)
        draft = context.user_data.get("nutrition_mealctx_draft") or {}
        context.user_data["nutrition_mealctx_step"] = step
        if step == 0:
            context.user_data["nutrition_state"] = "mealctx_type"
            options = (("breakfast", "Завтрак"), ("lunch", "Обед"), ("dinner", "Ужин"), ("snack", "Перекус"))
            rows = [[InlineKeyboardButton(
                f"Оставить: {draft.get('meal_type', 'прием пищи')}"[:60],
                callback_data=f"nutrition:mealctx_choice:{nonce}:type_keep",
            )]] + [
                [InlineKeyboardButton(label, callback_data=f"nutrition:mealctx_choice:{nonce}:type_{key}")]
                for key, label in options
            ]
            await self._reply(update, "Выберите тип приема пищи:",
                              reply_markup=InlineKeyboardMarkup(rows + self._mealctx_footer(nonce)))
        elif step == 1:
            context.user_data["nutrition_state"] = "mealctx_date"
            await self._reply(update, "Выберите дату приема пищи:", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"Оставить: {draft.get('local_date')}",
                    callback_data=f"nutrition:mealctx_choice:{nonce}:date_keep",
                )],
                [InlineKeyboardButton("Сегодня", callback_data=f"nutrition:mealctx_choice:{nonce}:date_today"),
                 InlineKeyboardButton("Вчера", callback_data=f"nutrition:mealctx_choice:{nonce}:date_yesterday")],
                [InlineKeyboardButton("Другая дата", callback_data=f"nutrition:mealctx_choice:{nonce}:date_other")],
            ] + self._mealctx_footer(nonce)))
        elif step == 2:
            context.user_data["nutrition_state"] = "mealctx_time"
            await self._reply(update, "Введите местное время в формате ЧЧ:ММ, например 13:30:",
                              reply_markup=InlineKeyboardMarkup([
                                  [InlineKeyboardButton(
                                      f"Оставить: {draft.get('local_time')}",
                                      callback_data=f"nutrition:mealctx_choice:{nonce}:time_keep",
                                  )],
                              ] + self._mealctx_footer(nonce)))
        elif step == 3:
            context.user_data["nutrition_state"] = "mealctx_note"
            await self._reply(update, "Введите необязательную заметку к приему:", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Без заметки", callback_data=f"nutrition:mealctx_choice:{nonce}:note_none")],
                [InlineKeyboardButton("Оставить текущую", callback_data=f"nutrition:mealctx_choice:{nonce}:note_keep")],
            ] + self._mealctx_footer(nonce)))
        elif step == 4:
            context.user_data["nutrition_state"] = "mealctx_hunger"
            rows = [[InlineKeyboardButton(str(i), callback_data=f"nutrition:mealctx_choice:{nonce}:hunger_{i}")
                     for i in range(start, min(start + 5, 11))] for start in (1, 6)]
            if draft.get("hunger_level") is not None:
                rows.insert(0, [InlineKeyboardButton(
                    f"Оставить: {draft['hunger_level']}",
                    callback_data=f"nutrition:mealctx_choice:{nonce}:hunger_keep",
                )])
            rows.append([InlineKeyboardButton("Не указывать", callback_data=f"nutrition:mealctx_choice:{nonce}:hunger_none")])
            await self._reply(update, "Голод перед едой от 1 до 10 (необязательно):",
                              reply_markup=InlineKeyboardMarkup(rows + self._mealctx_footer(nonce)))
        elif step == 5:
            context.user_data["nutrition_state"] = "mealctx_mood"
            rows = [[InlineKeyboardButton(label, callback_data=f"nutrition:mealctx_choice:{nonce}:mood_{key}")]
                    for key, label in self._MOODS.items()]
            if draft.get("mood") in self._MOODS:
                rows.insert(0, [InlineKeyboardButton(
                    f"Оставить: {self._MOODS[draft['mood']]}",
                    callback_data=f"nutrition:mealctx_choice:{nonce}:mood_keep",
                )])
            rows.append([InlineKeyboardButton("Не указывать", callback_data=f"nutrition:mealctx_choice:{nonce}:mood_none")])
            await self._reply(update, "Выберите настроение (необязательно):",
                              reply_markup=InlineKeyboardMarkup(rows + self._mealctx_footer(nonce)))
        else:
            context.user_data["nutrition_state"] = "mealctx_preview"
            mood = "не указано" if draft.get("mood") is None else self._MOODS[draft["mood"]]
            hunger = "не указан" if draft.get("hunger_level") is None else str(draft["hunger_level"])
            await self._reply(update,
                "Проверьте контекст приема:\n"
                f"Тип: {html.escape(str(draft['meal_type']))}\n"
                f"Дата и время: {draft['local_date']} {draft['local_time']}\n"
                f"Заметка: {html.escape(draft.get('note') or 'нет')}\n"
                f"Голод: {hunger}\nНастроение: {mood}",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Сохранить", callback_data=f"nutrition:mealctx_confirm:{nonce}")],
                ] + self._mealctx_footer(nonce)))

    @staticmethod
    def _mealctx_footer(nonce: str) -> list[list[InlineKeyboardButton]]:
        return [
            [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"nutrition:mealctx_back:{nonce}")],
            [InlineKeyboardButton("Отмена", callback_data="nutrition:menu")],
        ]

    async def _apply_meal_context_choice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str
    ) -> None:
        draft = context.user_data["nutrition_mealctx_draft"]
        step = int(context.user_data.get("nutrition_mealctx_step", 0))
        if choice == "type_keep" and step == 0:
            await self._show_meal_context_step(update, context, 1)
        elif choice.startswith("type_") and step == 0:
            types = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин", "snack": "перекус"}
            key = choice.removeprefix("type_")
            if key not in types:
                raise ValueError("Неизвестный тип приема пищи")
            draft["meal_type"] = types[key]
            await self._show_meal_context_step(update, context, 1)
        elif choice == "date_keep" and step == 1:
            await self._show_meal_context_step(update, context, 2)
        elif choice.startswith("date_") and step == 1:
            date_choice = choice.removeprefix("date_")
            if date_choice == "other":
                context.user_data["nutrition_state"] = "mealctx_date_other"
                await self._reply(update, "Введите дату ГГГГ-ММ-ДД:",
                                  reply_markup=InlineKeyboardMarkup(self._mealctx_footer(context.user_data['nutrition_mealctx_nonce'])))
            else:
                today = self._today(update.effective_user.id)
                if date_choice not in {"today", "yesterday"}:
                    raise ValueError("Неизвестный вариант даты")
                chosen = today if date_choice == "today" else today - timedelta(days=1)
                draft["local_date"] = chosen.isoformat()
                await self._show_meal_context_step(update, context, 2)
        elif choice == "time_keep" and step == 2:
            await self._show_meal_context_step(update, context, 3)
        elif choice in {"note_none", "note_keep"} and step == 3:
            if choice == "note_none":
                draft["note"] = ""
            await self._show_meal_context_step(update, context, 4)
        elif choice.startswith("hunger_") and step == 4:
            raw = choice.removeprefix("hunger_")
            if raw != "keep":
                draft["hunger_level"] = None if raw == "none" else int(raw)
            await self._show_meal_context_step(update, context, 5)
        elif choice.startswith("mood_") and step == 5:
            raw = choice.removeprefix("mood_")
            if raw not in {"none", "keep"} and raw not in self._MOODS:
                raise ValueError("Неизвестное настроение")
            if raw != "keep":
                draft["mood"] = None if raw == "none" else raw
            await self._show_meal_context_step(update, context, 6)
        else:
            raise PermissionError("Этот вариант относится к другому шагу")

    async def _back_meal_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        step = int(context.user_data.get("nutrition_mealctx_step", 0))
        if step <= 0:
            meal_id = int(context.user_data["nutrition_mealctx_meal_id"])
            meal = self.store.get_own_meal(client_telegram_id=update.effective_user.id, meal_id=meal_id)
            self._clear_mealctx_flow(context)
            await self._show_draft(update, context, meal)
        else:
            await self._show_meal_context_step(update, context, step - 1)

    async def _confirm_meal_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        draft = dict(context.user_data["nutrition_mealctx_draft"])
        user = self.store.get_user_by_telegram_id(update.effective_user.id)
        local = datetime.combine(
            date.fromisoformat(draft.pop("local_date")),
            datetime.strptime(draft.pop("local_time"), "%H:%M").time(),
            tzinfo=ZoneInfo(user["timezone"]),
        )
        meal_id = int(context.user_data["nutrition_mealctx_meal_id"])
        meal = self.store.update_meal_as_client(
            client_telegram_id=update.effective_user.id,
            meal_id=meal_id,
            updates={"eaten_at": local.isoformat(), **draft},
            expected_version=int(context.user_data["nutrition_mealctx_version"]),
        )
        self._clear_mealctx_flow(context)
        await self._show_draft(update, context, meal)

    @staticmethod
    def _clear_mealctx_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
        for key in (
            "nutrition_mealctx_nonce", "nutrition_mealctx_meal_id", "nutrition_mealctx_version",
            "nutrition_mealctx_draft", "nutrition_mealctx_step",
        ):
            context.user_data.pop(key, None)
        if str(context.user_data.get("nutrition_state", "")).startswith("mealctx_"):
            context.user_data.pop("nutrition_state", None)

    @staticmethod
    def _parse_times(text: str, *, limit: int) -> list[str]:
        values = [item.strip() for item in text.split(",") if item.strip()]
        if not values or len(values) > limit:
            raise ValueError(f"Укажите от 1 до {limit} значений времени через запятую")
        result = []
        for value in values:
            try:
                parsed = datetime.strptime(value, "%H:%M")
            except ValueError as exc:
                raise ValueError("Время нужно указать в формате ЧЧ:ММ") from exc
            normalized = parsed.strftime("%H:%M")
            if normalized not in result:
                result.append(normalized)
        return sorted(result)

    @classmethod
    def _parse_range(cls, text: str) -> tuple[str, str]:
        parts = [part.strip() for part in text.split("-")]
        if len(parts) != 2:
            raise ValueError("Укажите диапазон как ЧЧ:ММ-ЧЧ:ММ")
        return cls._parse_times(parts[0], limit=1)[0], cls._parse_times(parts[1], limit=1)[0]

    async def _show_client_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        prefs = self.store.get_client_reminder_preferences(telegram_id=update.effective_user.id)
        nonce = self._new_flow_nonce()
        context.user_data.update(
            nutrition_clientrem_nonce=nonce,
            nutrition_clientrem_version=prefs["version"],
            nutrition_clientrem_draft=copy.deepcopy(prefs),
            nutrition_state="clientrem_menu",
        )
        status = "включены" if prefs["enabled"] else "выключены"
        await self._reply(update,
            f"⏰ <b>Напоминания</b>\n\nСейчас {status}. Часовой пояс: {html.escape(prefs['timezone'])}. "
            "Это напоминания внести запись, они не утверждают, что вы не ели.",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Настроить", callback_data=f"nutrition:clientrem_start:{nonce}")],
                [InlineKeyboardButton("🔕 Выключить все", callback_data=f"nutrition:clientrem_choice:{nonce}:disable")],
                [InlineKeyboardButton("⬅️ В дневник", callback_data="nutrition:menu")],
            ]))

    async def _show_client_reminder_step(self, update: Update, context: ContextTypes.DEFAULT_TYPE, step: int) -> None:
        nonce = context.user_data["nutrition_clientrem_nonce"]
        draft = context.user_data["nutrition_clientrem_draft"]
        context.user_data["nutrition_clientrem_step"] = step
        footer = [[InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
                  [InlineKeyboardButton("⬅️ Назад", callback_data=f"nutrition:clientrem_back:{nonce}")],
                  [InlineKeyboardButton("Отмена", callback_data="nutrition:reminders")]]
        if step == 0:
            context.user_data["nutrition_state"] = "clientrem_meal"
            rows = [[InlineKeyboardButton("Напоминать", callback_data=f"nutrition:clientrem_choice:{nonce}:meal_on")],
                    [InlineKeyboardButton("Не напоминать", callback_data=f"nutrition:clientrem_choice:{nonce}:meal_off")]]
            await self._reply(update, "Напоминать о внесении приемов пищи?", reply_markup=InlineKeyboardMarkup(rows + footer))
        elif step == 1:
            context.user_data["nutrition_state"] = "clientrem_meal_times"
            await self._reply(update, "Введите до трех местных времен через запятую, например 08:00, 13:00, 19:00:", reply_markup=InlineKeyboardMarkup(footer))
        elif step == 2:
            context.user_data["nutrition_state"] = "clientrem_water"
            rows = [[InlineKeyboardButton("Не напоминать", callback_data=f"nutrition:clientrem_choice:{nonce}:water_off")],
                    [InlineKeyboardButton("В заданное время", callback_data=f"nutrition:clientrem_choice:{nonce}:water_times")],
                    [InlineKeyboardButton("С интервалом", callback_data=f"nutrition:clientrem_choice:{nonce}:water_interval")]]
            await self._reply(update, "Как напоминать внести воду?", reply_markup=InlineKeyboardMarkup(rows + footer))
        elif step == 3:
            context.user_data["nutrition_state"] = "clientrem_water_value"
            text = ("Введите времена через запятую, например 10:00, 15:00, 20:00:"
                    if draft["water"]["mode"] == "times" else "Введите интервал от 60 до 720 минут, например 120:")
            await self._reply(update, text, reply_markup=InlineKeyboardMarkup(footer))
        elif step == 4 and draft["water"]["enabled"] and draft["water"]["mode"] == "interval" and not draft["water"].get("start_local"):
            context.user_data["nutrition_state"] = "clientrem_water_range"
            await self._reply(update, "Введите границы дня по местному времени, например 08:00-22:00:", reply_markup=InlineKeyboardMarkup(footer))
        elif step == 4:
            context.user_data["nutrition_state"] = "clientrem_quiet"
            rows = [[InlineKeyboardButton("Без тихих часов", callback_data=f"nutrition:clientrem_choice:{nonce}:quiet_off")]]
            await self._reply(update, "Введите тихие часы, например 22:00-07:00, или отключите их:", reply_markup=InlineKeyboardMarkup(rows + footer))
        else:
            context.user_data["nutrition_state"] = "clientrem_preview"
            draft["enabled"] = bool(draft["meal"]["enabled"] or draft["water"]["enabled"])
            await self._reply(update, self._format_client_reminders(draft, preview=True), reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Сохранить", callback_data=f"nutrition:clientrem_confirm:{nonce}")], *footer]))

    async def _client_reminder_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str) -> None:
        draft = context.user_data["nutrition_clientrem_draft"]
        if choice == "disable" and context.user_data.get("nutrition_state") == "clientrem_menu":
            draft.update(enabled=False, meal={"enabled": False, "times": []}, water={"enabled": False, "mode": "times", "times": [], "interval_minutes": None, "start_local": None, "end_local": None})
            draft["quiet_hours"] = {"start": None, "end": None}
            await self._show_client_reminder_step(update, context, 5)
        elif choice.startswith("meal_") and context.user_data["nutrition_state"] == "clientrem_meal":
            enabled = choice == "meal_on"; draft["meal"]["enabled"] = enabled
            await self._show_client_reminder_step(update, context, 1 if enabled else 2)
        elif choice.startswith("water_") and context.user_data["nutrition_state"] == "clientrem_water":
            mode = choice.removeprefix("water_")
            draft["water"].update(enabled=mode != "off", mode="interval" if mode == "interval" else "times", times=[], interval_minutes=None, start_local=None, end_local=None)
            await self._show_client_reminder_step(update, context, 3 if mode != "off" else 4)
        elif choice == "quiet_off" and context.user_data["nutrition_state"] == "clientrem_quiet":
            draft["quiet_hours"] = {"start": None, "end": None}
            await self._show_client_reminder_step(update, context, 5)
        else:
            raise PermissionError("Эта кнопка относится к другому шагу")

    async def _back_client_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        state = context.user_data.get("nutrition_state")
        steps = {"clientrem_meal": None, "clientrem_meal_times": 0, "clientrem_water": 1 if context.user_data["nutrition_clientrem_draft"]["meal"]["enabled"] else 0, "clientrem_water_value": 2, "clientrem_water_range": 3, "clientrem_quiet": 3 if context.user_data["nutrition_clientrem_draft"]["water"]["enabled"] else 2, "clientrem_preview": 4}
        step = steps.get(state)
        if step is None:
            await self._show_client_reminders(update, context)
        else:
            await self._show_client_reminder_step(update, context, step)

    async def _confirm_client_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        draft = context.user_data["nutrition_clientrem_draft"]
        result = self.store.update_client_reminder_preferences(telegram_id=update.effective_user.id, updates={k: draft[k] for k in ("enabled", "meal", "water", "quiet_hours")}, expected_version=context.user_data["nutrition_clientrem_version"], idempotency_key=f"clientrem:{update.effective_user.id}:{context.user_data['nutrition_clientrem_nonce']}")
        await self._reply(update, "✅ Настройки сохранены.\n\n" + self._format_client_reminders(result), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В дневник", callback_data="nutrition:menu")]]))

    @staticmethod
    def _format_client_reminders(prefs: dict[str, Any], *, preview: bool = False) -> str:
        meal = ", ".join(prefs["meal"]["times"]) if prefs["meal"]["enabled"] else "выключены"
        water = "выключены"
        if prefs["water"]["enabled"]:
            water = (", ".join(prefs["water"]["times"]) if prefs["water"]["mode"] == "times" else f"каждые {prefs['water']['interval_minutes']} мин, {prefs['water']['start_local']}-{prefs['water']['end_local']}")
        quiet = "нет" if prefs["quiet_hours"]["start"] is None else f"{prefs['quiet_hours']['start']}-{prefs['quiet_hours']['end']}"
        return ("Проверьте расписание:\n" if preview else "Расписание:\n") + f"Еда: {meal}\nВода: {water}\nТихие часы: {quiet}\nЧасовой пояс: {prefs['timezone']}"

    async def _show_trainer_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        prefs = self.store.get_trainer_reminder_preferences(trainer_telegram_id=update.effective_user.id)
        nonce = self._new_flow_nonce()
        context.user_data.update(nutrition_trainerrem_nonce=nonce, nutrition_trainerrem_version=prefs["version"], nutrition_trainerrem_draft=copy.deepcopy(prefs), nutrition_state="trainerrem_menu")
        await self._reply(update, self._format_trainer_reminders(prefs), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Настроить", callback_data=f"nutrition:trainerrem_start:{nonce}")],
            [InlineKeyboardButton("🔕 Выключить", callback_data=f"nutrition:trainerrem_choice:{nonce}:disable")],
            [InlineKeyboardButton("⬅️ Кабинет", callback_data="nutrition:trainer")],
        ]))

    async def _show_trainer_reminder_step(self, update: Update, context: ContextTypes.DEFAULT_TYPE, step: int) -> None:
        nonce = context.user_data["nutrition_trainerrem_nonce"]
        draft = context.user_data["nutrition_trainerrem_draft"]
        context.user_data["nutrition_trainerrem_step"] = step
        footer = [[InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")], [InlineKeyboardButton("Отмена", callback_data="nutrition:trainer_reminders")]]
        if step == 0:
            context.user_data["nutrition_state"] = "trainerrem_time"
            await self._reply(update, "Введите местное время ежедневной сводки, например 20:00:", reply_markup=InlineKeyboardMarkup(footer))
        elif step == 1:
            context.user_data["nutrition_state"] = "trainerrem_days"
            await self._reply(update, "Через сколько дней без записей отметить отсутствие активности? Введите 1-7. Это не означает, что клиент не ел:", reply_markup=InlineKeyboardMarkup(footer))
        elif step == 2:
            context.user_data["nutrition_state"] = "trainerrem_compare"
            rows = [[InlineKeyboardButton("Сравнивать с планом", callback_data=f"nutrition:trainerrem_choice:{nonce}:compare_on")], [InlineKeyboardButton("Не сравнивать", callback_data=f"nutrition:trainerrem_choice:{nonce}:compare_off")]]
            await self._reply(update, "Включить сравнение подтвержденных калорий с нормой?", reply_markup=InlineKeyboardMarkup(rows + footer))
        else:
            context.user_data["nutrition_state"] = "trainerrem_preview"
            await self._reply(update, "Проверьте настройки:\n" + self._format_trainer_reminders(draft), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Сохранить", callback_data=f"nutrition:trainerrem_confirm:{nonce}")]] + footer))

    async def _trainer_reminder_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE, choice: str) -> None:
        draft = context.user_data["nutrition_trainerrem_draft"]
        if choice == "disable" and context.user_data.get("nutrition_state") == "trainerrem_menu":
            draft["enabled"] = False; draft["daily_digest"]["enabled"] = False
            await self._show_trainer_reminder_step(update, context, 3)
        elif choice == "compare_off" and context.user_data["nutrition_state"] == "trainerrem_compare":
            draft["daily_digest"]["calorie_comparison_enabled"] = False
            await self._show_trainer_reminder_step(update, context, 3)
        elif choice == "compare_on" and context.user_data["nutrition_state"] == "trainerrem_compare":
            draft["daily_digest"]["calorie_comparison_enabled"] = True
            context.user_data["nutrition_state"] = "trainerrem_percent"
            await self._reply(update, "Введите порог превышения нормы от 0 до 500 процентов, например 10:", reply_markup=self._cancel_keyboard("nutrition:trainer_reminders"))
        else:
            raise PermissionError("Эта кнопка относится к другому шагу")

    async def _confirm_trainer_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        draft = context.user_data["nutrition_trainerrem_draft"]
        result = self.store.update_trainer_reminder_preferences(trainer_telegram_id=update.effective_user.id, updates={"enabled": draft["enabled"], "daily_digest": draft["daily_digest"]}, expected_version=context.user_data["nutrition_trainerrem_version"], idempotency_key=f"trainerrem:{update.effective_user.id}:{context.user_data['nutrition_trainerrem_nonce']}")
        await self._reply(update, "✅ Настройки сводки сохранены.\n\n" + self._format_trainer_reminders(result), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Кабинет", callback_data="nutrition:trainer")]]))

    @staticmethod
    def _format_trainer_reminders(prefs: dict[str, Any]) -> str:
        digest = prefs["daily_digest"]
        if not prefs["enabled"] or not digest["enabled"]:
            return f"🔔 Сводка тренера выключена. Часовой пояс: {prefs['timezone']}."
        compare = f"порог {digest['over_plan_percent']}%" if digest["calorie_comparison_enabled"] else "выключено"
        return f"🔔 Сводка: {digest['time']}; отсутствие записей: {digest['inactivity_days']} дн.; сравнение с нормой: {compare}; часовой пояс: {prefs['timezone']}."

    async def _show_browser_cabinet_link(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.dashboard_origin.startswith("https://"):
            raise ValueError("Веб-кабинет пока не подключен")
        login = self.store.create_browser_login_token(telegram_id=update.effective_user.id)
        url = f"{self.dashboard_origin}/login#token={quote(login['token'], safe='')}"
        await self._reply(
            update,
            "Одноразовая ссылка действует 10 минут и открывает только ваш кабинет.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Открыть кабинет", url=url)],
                [InlineKeyboardButton("⬅️ В дневник", callback_data="nutrition:menu")],
            ]),
        )


    async def _show_draft(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        meal: dict[str, Any],
    ) -> None:
        context.user_data["nutrition_state"] = "draft"
        context.user_data["nutrition_draft_meal_id"] = meal["id"]
        moment = datetime.fromisoformat(meal["eaten_at"]).astimezone(ZoneInfo(meal["timezone"]))
        lines = [
            f"🍽️ <b>Черновик #{meal['id']}</b>",
            f"{html.escape(meal['meal_type'])}, {moment:%d.%m.%Y %H:%M}",
        ]
        lines.extend(self._meal_context_details(meal))
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
            [InlineKeyboardButton(
                "📝 Контекст приема", callback_data=f"nutrition:meal_context:{meal['id']}"
            )],
            [InlineKeyboardButton(confirm_label, callback_data=f"nutrition:confirm:{meal['id']}")],
            [InlineKeyboardButton("✏️ Исправить вручную", callback_data=f"nutrition:edit:{meal['id']}")],
            [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
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
            [InlineKeyboardButton("⏰ Напоминания", callback_data="nutrition:reminders")],
            [
                InlineKeyboardButton("👤 Профиль", callback_data="nutrition:profile"),
                InlineKeyboardButton("⚖️ Вес", callback_data="nutrition:weight"),
            ],
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
        if self.dashboard_origin:
            buttons.append([
                InlineKeyboardButton("🌐 Веб-кабинет", callback_data="nutrition:cabinet_link")
            ])
        buttons.append([InlineKeyboardButton("❓ Помощь", callback_data="nutrition:help")])
        buttons.append([InlineKeyboardButton("Закрыть дневник", callback_data="nutrition:leave")])
        text = (
            "🥗 <b>Дневник питания</b>\n\n"
            "Фото и текст создают черновик. Оценка ИИ приблизительная и требует "
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
        keyboard.append([InlineKeyboardButton("🔔 Ежедневная сводка", callback_data="nutrition:trainer_reminders")])
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
                [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
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
                    [InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")],
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
        buttons.append([InlineKeyboardButton("❓ Помощь по шагу", callback_data="nutrition:help")])
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
        lines.extend(NutritionBotController._meal_context_details(meal))
        if include_comments:
            for comment in meal["comments"]:
                lines.append(f"💬 {html.escape(comment['text'])}")
        return "\n".join(lines)

    @staticmethod
    def _meal_context_details(meal: dict[str, Any]) -> list[str]:
        lines = []
        if meal.get("note"):
            lines.append(f"📝 {html.escape(str(meal['note']))}")
        if meal.get("hunger_level") is not None:
            lines.append(f"Голод перед едой: {int(meal['hunger_level'])}/10")
        moods = {
            "great": "отличное", "good": "хорошее", "neutral": "нейтральное",
            "low": "пониженное", "stressed": "напряженное",
        }
        if meal.get("mood") in moods:
            lines.append(f"Настроение: {moods[meal['mood']]}")
        return lines

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
                [InlineKeyboardButton("❓ Помощь", callback_data="nutrition:help")],
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
            telegram_username=getattr(user, "username", None),
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
            for detail in NutritionBotController._meal_context_details(meal):
                lines.append(f"  {detail}")
        if not summary["meals"]:
            lines.append("Записей о приемах пищи нет. Это не означает, что человек не ел.")
        totals = summary["totals"]
        lines.append(
            f"\nИтого по записям: {totals['calories']:g} ккал, "
            f"Б {totals['protein_g']:g}, Ж {totals['fat_g']:g}, У {totals['carbs_g']:g}"
        )
        if summary.get("water_entries"):
            lines.append(f"💧 Вода по записям: {summary['water_ml']} мл")
        else:
            lines.append("💧 Записей о воде нет.")
        if summary.get("weight_kg") is not None:
            lines.append(f"⚖️ Последний вес за день: {summary['weight_kg']:g} кг")
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
            water = (
                f"вода {day['water_ml']} мл"
                if day.get("water_entries")
                else "нет записей о воде"
            )
            lines.append(f"• {day['date']}: {marker}, {water}")
        lines.append(
            "\nСреднее за 7 календарных дней рассчитано только из внесенных записей. "
            "Пустой день не означает отсутствие еды."
        )
        averages = summary["averages"]
        coverage = summary.get("coverage") or {}
        if averages.get("calories") is None:
            lines.append("Среднее по питанию: нет подтвержденных записей.")
        else:
            lines.append(
                f"Среднее по дням с едой ({coverage.get('nutrition_logged_days', 0)}): "
                f"{averages['calories']:g} ккал, Б {averages['protein_g']:g}, "
                f"Ж {averages['fat_g']:g}, У {averages['carbs_g']:g}."
            )
        if averages.get("water_ml") is None:
            lines.append("Среднее по воде: нет записей.")
        else:
            lines.append(
                f"Среднее по дням с водой ({coverage.get('water_logged_days', 0)}): "
                f"{averages['water_ml']:g} мл."
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
