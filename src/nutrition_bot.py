"""Telegram-сценарии дневника питания.

Контроллер подключается к существующему TelegramSMMBot и обрабатывает только
callback с префиксом ``nutrition:`` и сообщения активной nutrition-сессии.
Все записи сначала создаются как черновик и попадают в статистику лишь после
явного подтверждения пользователя.
"""

from __future__ import annotations

import asyncio
import html
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from src.nutrition_ai import NutritionAI, NutritionAIResponseError, NutritionAIUnavailable
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

    def __init__(
        self,
        *,
        store: NutritionStore,
        ai: NutritionAI | None = None,
        trainer_ids: set[int] | None = None,
        dashboard_origin: str | None = None,
    ):
        self.store = store
        self.ai = ai or NutritionAI()
        self.trainer_ids = trainer_ids if trainer_ids is not None else parse_trainer_ids()
        # Веб-кнопка появляется только после явного успешного bootstrap панели.
        # Значение из окружения проверяет интеграционный слой TelegramSMMBot.
        self.dashboard_origin = (dashboard_origin or "").rstrip("/")

    @staticmethod
    def is_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
        return bool(context.user_data.get("nutrition_active"))

    def is_trainer(self, telegram_id: int) -> bool:
        return telegram_id in self.trainer_ids

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._private_only(update, context):
            return
        self._ensure_user(update)
        context.user_data["nutrition_active"] = True
        context.user_data.pop("nutrition_state", None)
        await self._send_menu(update, context)

    async def show_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показывает личную дневную сводку для команды /today."""
        if not await self._private_only(update, context):
            return
        self._ensure_user(update)
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
                context.user_data.pop("nutrition_state", None)
                await self._send_menu(update, context, edit=True)
            elif data == "nutrition:add_meal":
                keyboard = [
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
            elif data == "nutrition:meal_photo":
                context.user_data["nutrition_state"] = "wait_photo"
                await query.edit_message_text(
                    "📸 Пришлите одно фото еды. Сначала будет создан черновик, "
                    "который вы сможете исправить, подтвердить или отменить."
                )
            elif data == "nutrition:meal_manual":
                context.user_data["nutrition_state"] = "manual_only"
                await query.edit_message_text(
                    "Ручной ввод без AI.\n\n" + self._manual_prompt()
                )
            elif data == "nutrition:meal_text_ai":
                context.user_data["nutrition_state"] = "wait_manual"
                await query.edit_message_text(
                    "Ваше описание будет передано OpenAI для приблизительной оценки. "
                    "После этого вы проверите черновик. Опишите еду и размер порции:"
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
                    self._manual_prompt()
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
                    "Название; масса в г или описание порции; ккал; белки; жиры; углеводы"
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
                await query.edit_message_text("Введите объем воды целым числом в миллилитрах:")
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
                await query.edit_message_text("Введите код, который прислал ваш тренер:")
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
                    "Введите часовой пояс в формате IANA, например Europe/Moscow или Asia/Tokyo:"
                )
            elif data == "nutrition:trainer":
                self._require_trainer(update.effective_user.id)
                await self._show_trainer_clients(update)
            elif data == "nutrition:invite":
                self._require_trainer(update.effective_user.id)
                invite = self.store.create_trainer_invite(trainer_telegram_id=update.effective_user.id)
                await query.edit_message_text(
                    "Код для привязки клиента:\n\n"
                    f"<code>{html.escape(invite['code'])}</code>\n\n"
                    "Код одноразовый и действует 7 дней.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Кабинет", callback_data="nutrition:trainer")]]
                    ),
                )
            elif data.startswith("nutrition:trainer_day:"):
                self._require_trainer(update.effective_user.id)
                client_id = int(data.rsplit(":", 1)[1])
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
                context.user_data["nutrition_state"] = "trainer_norms"
                context.user_data["nutrition_trainer_client_id"] = client_id
                await query.edit_message_text(
                    "Введите нормы через точку с запятой:\n"
                    "YYYY-MM-DD; ккал; белки; жиры; углеводы; вода мл\n\n"
                    "Дата определяет первый день действия. Старые дни не изменятся."
                )
            elif data.startswith("nutrition:trainer_edit:"):
                self._require_trainer(update.effective_user.id)
                context.user_data["nutrition_state"] = "trainer_edit"
                context.user_data["nutrition_trainer_client_id"] = int(data.rsplit(":", 1)[1])
                await query.edit_message_text(
                    "Первая строка: ID приема пищи. Далее блюда по одному на строку:\n"
                    "Название; масса в г или описание порции; ккал; белки; жиры; углеводы"
                )
            elif data.startswith("nutrition:trainer_comment:"):
                self._require_trainer(update.effective_user.id)
                context.user_data["nutrition_state"] = "trainer_comment"
                context.user_data["nutrition_trainer_client_id"] = int(data.rsplit(":", 1)[1])
                await query.edit_message_text("Введите: ID приема пищи; комментарий")
            elif data == "nutrition:leave":
                self._clear(context)
                await query.edit_message_text("Режим дневника закрыт. Используйте кнопки основного меню.")
            else:
                return False
        except (ValueError, PermissionError) as exc:
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

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if not self.is_active(context) or not update.message or not update.message.text:
            return False
        if not await self._private_only(update, context):
            return True
        self._ensure_user(update)
        state = context.user_data.get("nutrition_state")
        text = update.message.text.strip()
        try:
            if state == "wait_manual":
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
            elif state == "trainer_edit":
                self._require_trainer(update.effective_user.id)
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if len(lines) < 2:
                    raise ValueError("Нужен ID приема и хотя бы одно блюдо")
                meal_id = int(lines[0])
                items = self.parse_structured_items("\n".join(lines[1:]))
                meal = self.store.update_meal_as_trainer(
                    update.effective_user.id, meal_id, {"items": items}
                )
                context.user_data.pop("nutrition_state", None)
                await update.message.reply_text(f"✅ Прием пищи #{meal['id']} исправлен.")
            elif state == "trainer_comment":
                self._require_trainer(update.effective_user.id)
                meal_raw, separator, comment = text.partition(";")
                if not separator:
                    raise ValueError("Используйте формат: ID; комментарий")
                result = self.store.add_comment(
                    update.effective_user.id, int(meal_raw.strip()), comment.strip()
                )
                context.user_data.pop("nutrition_state", None)
                await update.message.reply_text(f"✅ Комментарий #{result['id']} добавлен.")
            else:
                await self._send_menu(update, context)
            return True
        except (ValueError, PermissionError, NutritionAIResponseError) as exc:
            await update.message.reply_text(f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
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
        await self._create_draft(
            update, context, result["items"], source=source, photo_file_id=photo_file_id
        )
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
        if approximate:
            lines.append("\n⚠️ Оценка приблизительная. Проверьте блюдо и размер порции.")
        lines.append("\nЗапись попадет в статистику только после подтверждения.")
        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить", callback_data=f"nutrition:confirm:{meal['id']}")],
            [InlineKeyboardButton("✏️ Исправить", callback_data=f"nutrition:edit:{meal['id']}")],
            [InlineKeyboardButton("Отменить", callback_data=f"nutrition:cancel:{meal['id']}")],
        ]
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
        keyboard.append([InlineKeyboardButton("➕ Код для клиента", callback_data="nutrition:invite")])
        if self.dashboard_origin:
            keyboard.append(
                [InlineKeyboardButton("🌐 Веб-панель", web_app=WebAppInfo(url=self.dashboard_origin))]
            )
        keyboard.append([InlineKeyboardButton("⬅️ В дневник", callback_data="nutrition:menu")])
        message = "👥 <b>Клиенты тренера</b>"
        if not clients:
            message += "\n\nПока нет привязанных клиентов. Создайте одноразовый код."
        await update.callback_query.edit_message_text(
            message, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
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
            suffix = f" (ID {meal['id']})" if trainer else ""
            lines.append(f"• {html.escape(meal['meal_type'])}: {total:g} ккал{suffix}")
            if trainer:
                for item in meal["items"]:
                    lines.append(f"  · {html.escape(item['name'])}, item {item['id']}")
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
            lines.append(
                f"Нормы с {norms['effective_from']}: {norms['calories'] or 'не задано'} ккал, "
                f"вода {norms['water_ml'] or 'не задано'} мл"
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
