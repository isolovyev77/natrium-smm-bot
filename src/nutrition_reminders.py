"""Ограниченный планировщик opt-in напоминаний дневника питания."""

from __future__ import annotations

import asyncio
import inspect
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .nutrition_store import NutritionStore


ReminderSender = Callable[[int, str], Any]


class ReminderLoop:
    """Независимый от Telegram lifecycle для одного bounded tick за интервал."""

    def __init__(
        self,
        engine: "ReminderEngine",
        *,
        interval_seconds: float = 60,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Интервал планировщика должен быть положительным")
        self.engine = engine
        self.interval_seconds = interval_seconds
        self.on_error = on_error
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="nutrition-reminders")

    async def stop(self) -> None:
        if self._task is None:
            return
        task = self._task
        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                await self.engine.tick_async()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.on_error is not None:
                    self.on_error(exc)
            await asyncio.sleep(self.interval_seconds)


class ReminderEngine:
    """Создает только свежие слоты, затем перепроверяет настройки перед отправкой."""

    def __init__(
        self,
        store: NutritionStore,
        sender: ReminderSender,
        *,
        allowed_trainer_ids: set[int],
        grace_seconds: int = 600,
        batch_size: int = 50,
        source_batch_size: int = 500,
    ) -> None:
        if grace_seconds < 60 or grace_seconds > 3600:
            raise ValueError("Окно доставки должно быть от 60 до 3600 секунд")
        if batch_size < 1 or batch_size > 100:
            raise ValueError("Размер пачки должен быть от 1 до 100")
        if source_batch_size < 1 or source_batch_size > 500:
            raise ValueError("Размер обхода настроек должен быть от 1 до 500")
        self.store = store
        self.sender = sender
        self.allowed_trainer_ids = {int(value) for value in allowed_trainer_ids}
        self.grace_seconds = grace_seconds
        self.batch_size = batch_size
        self.source_batch_size = source_batch_size
        self._client_cursor = 0
        self._trainer_cursor = 0

    def tick(self, *, now: datetime | None = None) -> dict[str, int]:
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        scheduled = self.schedule_due(now=instant)
        delivered = self.dispatch_due(now=instant)
        return {"scheduled": scheduled, **delivered}

    async def tick_async(self, *, now: datetime | None = None) -> dict[str, int]:
        instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        scheduled = self.schedule_due(now=instant)
        delivered = await self.dispatch_due_async(now=instant)
        return {"scheduled": scheduled, **delivered}

    def schedule_due(self, *, now: datetime) -> int:
        instant = now.astimezone(timezone.utc)
        created = 0
        client_sources = self.store.list_client_reminder_sources(
            after_user_id=self._client_cursor, limit=self.source_batch_size
        )
        if client_sources:
            self._client_cursor = int(client_sources[-1]["user_id"])
            if len(client_sources) < self.source_batch_size:
                self._client_cursor = 0
        else:
            self._client_cursor = 0
        for source in client_sources:
            for kind, local_day, slot, scheduled_for in self._client_slots(source, instant):
                created += int(self.store.enqueue_notification(
                    recipient_user_id=source["user_id"],
                    subject_user_id=source["user_id"],
                    kind=kind,
                    local_date=local_day,
                    slot_key=slot,
                    preferences_scope="client",
                    preferences_version=source["version"],
                    scheduled_for=scheduled_for,
                ))
        trainer_sources = self.store.list_trainer_reminder_sources(
            after_user_id=self._trainer_cursor, limit=self.source_batch_size
        )
        if trainer_sources:
            self._trainer_cursor = int(trainer_sources[-1]["user_id"])
            if len(trainer_sources) < self.source_batch_size:
                self._trainer_cursor = 0
        else:
            self._trainer_cursor = 0
        for source in trainer_sources:
            if int(source["telegram_id"]) not in self.allowed_trainer_ids:
                continue
            due = self._due_slot(
                source["timezone"], source["digest_time"], instant,
                quiet_start=None, quiet_end=None,
            )
            if due is not None:
                local_day, scheduled_for = due
                created += int(self.store.enqueue_notification(
                    recipient_user_id=source["user_id"],
                    subject_user_id=source["user_id"],
                    kind="trainer_digest",
                    local_date=local_day,
                    slot_key=source["digest_time"],
                    preferences_scope="trainer",
                    preferences_version=source["version"],
                    scheduled_for=scheduled_for,
                ))
        return created

    def dispatch_due(self, *, now: datetime) -> dict[str, int]:
        jobs = self.store.claim_due_notifications(
            now=now, grace_seconds=self.grace_seconds, limit=self.batch_size
        )
        result = {"sent": 0, "cancelled": 0, "failed": 0}
        for claimed in jobs:
            job = self.store.validate_notification_for_delivery(
                notification_id=claimed["id"],
                allowed_trainer_ids=self.allowed_trainer_ids,
            )
            if job is None:
                result["cancelled"] += 1
                continue
            try:
                text = self._message(job)
                self.sender(int(job["telegram_id"]), text)
            except Exception as exc:
                self.store.complete_notification(
                    notification_id=job["id"], sent=False,
                    error_code=type(exc).__name__,
                )
                result["failed"] += 1
            else:
                self.store.complete_notification(notification_id=job["id"], sent=True)
                result["sent"] += 1
        return result

    async def dispatch_due_async(self, *, now: datetime) -> dict[str, int]:
        jobs = self.store.claim_due_notifications(
            now=now, grace_seconds=self.grace_seconds, limit=self.batch_size
        )
        result = {"sent": 0, "cancelled": 0, "failed": 0}
        for claimed in jobs:
            job = self.store.validate_notification_for_delivery(
                notification_id=claimed["id"],
                allowed_trainer_ids=self.allowed_trainer_ids,
            )
            if job is None:
                result["cancelled"] += 1
                continue
            try:
                pending = self.sender(int(job["telegram_id"]), self._message(job))
                if inspect.isawaitable(pending):
                    await pending
            except Exception as exc:
                self.store.complete_notification(
                    notification_id=job["id"], sent=False,
                    error_code=type(exc).__name__,
                )
                result["failed"] += 1
            else:
                self.store.complete_notification(notification_id=job["id"], sent=True)
                result["sent"] += 1
        return result

    def _client_slots(
        self, source: dict[str, Any], now: datetime
    ) -> list[tuple[str, str, str, datetime]]:
        result: list[tuple[str, str, str, datetime]] = []
        common = (source["quiet_start"], source["quiet_end"])
        if source["meal_enabled"]:
            for slot in source["meal_times"]:
                due = self._due_slot(source["timezone"], slot, now, *common)
                if due is not None:
                    result.append(("meal", due[0], slot, due[1]))
        if source["water_enabled"]:
            slots = list(source["water_times"])
            if source["water_mode"] == "interval":
                slots = self._interval_slots(
                    source["water_start_local"], source["water_end_local"],
                    int(source["water_interval_minutes"]),
                )
            for slot in slots:
                due = self._due_slot(source["timezone"], slot, now, *common)
                if due is not None:
                    result.append(("water", due[0], slot, due[1]))
        return result

    def _due_slot(
        self,
        timezone_name: str,
        slot: str,
        now: datetime,
        quiet_start: str | None,
        quiet_end: str | None,
    ) -> tuple[str, datetime] | None:
        zone = ZoneInfo(timezone_name)
        local_now = now.astimezone(zone)
        hour, minute = (int(part) for part in slot.split(":"))
        local_scheduled = datetime.combine(
            local_now.date(), time(hour, minute), tzinfo=zone
        )
        scheduled = local_scheduled.astimezone(timezone.utc)
        oldest = now - timedelta(seconds=self.grace_seconds)
        if not oldest <= scheduled <= now:
            return None
        if self._in_quiet_hours(slot, quiet_start, quiet_end):
            return None
        return local_scheduled.date().isoformat(), scheduled

    @staticmethod
    def _interval_slots(start: str, end: str, minutes: int) -> list[str]:
        start_hour, start_minute = (int(part) for part in start.split(":"))
        end_hour, end_minute = (int(part) for part in end.split(":"))
        cursor = datetime.combine(date(2000, 1, 1), time(start_hour, start_minute))
        finish = datetime.combine(date(2000, 1, 1), time(end_hour, end_minute))
        if finish <= cursor:
            return []
        result = []
        while cursor <= finish and len(result) < 24:
            result.append(cursor.strftime("%H:%M"))
            cursor += timedelta(minutes=minutes)
        return result

    @staticmethod
    def _in_quiet_hours(value: str, start: str | None, end: str | None) -> bool:
        if start is None or end is None:
            return False
        if start < end:
            return start <= value < end
        return value >= start or value < end

    def _message(self, job: dict[str, Any]) -> str:
        if job["kind"] == "meal":
            return "Напоминание: если сегодня был прием пищи, его можно добавить в дневник."
        if job["kind"] == "water":
            return "Напоминание: при желании отметьте воду в дневнике."
        return self._trainer_digest_text(int(job["telegram_id"]), job["local_date"])

    def _trainer_digest_text(self, trainer_telegram_id: int, local_date: str) -> str:
        prefs = self.store.get_trainer_reminder_preferences(
            trainer_telegram_id=trainer_telegram_id
        )["daily_digest"]
        clients = self.store.list_trainer_clients(trainer_telegram_id)
        inactive: list[str] = []
        above_plan: list[str] = []
        for client in clients:
            client_today = date.fromisoformat(client["local_today"])
            start = client_today - timedelta(days=int(prefs["inactivity_days"]) - 1)
            summary = self.store.get_client_summary(
                trainer_telegram_id, client["id"], start, client_today
            )
            if summary["coverage"]["calories"]["logged_days"] == 0:
                inactive.append(client["display_name"])
            if prefs["calorie_comparison_enabled"]:
                today = client["today"]
                norms = self.store.get_client_day(
                    trainer_telegram_id, client["id"], client_today
                ).get("norms")
                planned = float(norms["calories"]) if norms and norms["calories"] is not None else None
                actual = float(today["totals"]["calories"])
                threshold = 1 + int(prefs["over_plan_percent"]) / 100
                if planned is not None and planned > 0 and actual > planned * threshold:
                    above_plan.append(client["display_name"])
        lines = [f"Сводка дневника за {local_date}."]
        lines.append(
            f"Без записей питания за выбранный период: {', '.join(inactive)}."
            if inactive else "У всех связанных клиентов есть записи питания за выбранный период."
        )
        if prefs["calorie_comparison_enabled"]:
            lines.append(
                f"Выше заданной нормы на выбранный порог: {', '.join(above_plan)}."
                if above_plan else "Превышений заданной нормы на выбранный порог сегодня нет."
            )
        return "\n".join(lines)
