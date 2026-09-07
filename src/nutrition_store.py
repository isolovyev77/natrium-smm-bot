"""Хранилище дневника питания на SQLite.

Публичный контракт рассчитан одновременно на Telegram-бота и панель тренера.
Все методы панели принимают ``trainer_telegram_id`` и проверяют явную связь
тренера с клиентом внутри транзакции. Чужой клиент или прием пищи приводит к
``PermissionError``. Ошибочные данные приводят к ``ValueError``.

Основные возвращаемые формы:

``list_trainer_clients(trainer_telegram_id)``
    ``[{id, telegram_id, display_name, timezone}]``

``get_client_day(trainer_telegram_id, client_id, local_date)``
    ``{client, date, norms, meals, water_ml, totals}``, где ``meals`` содержит
    подтвержденные приемы пищи и их ``items``.

``get_client_week(trainer_telegram_id, client_id, week_start)``
    ``{client, week_start, days, averages}``, где ``days`` содержит семь
    дневных сводок.

``update_meal_as_trainer(trainer_telegram_id, meal_id, updates)``
    Обновляет поля приема ``eaten_at``, ``meal_type``, ``note`` или полностью
    заменяет ``items``. Возвращает обновленный прием.

``set_norms(trainer_telegram_id, client_id, norms, effective_from)``
    Создает или заменяет норму с датой начала действия и возвращает запись.

``add_comment(trainer_telegram_id, meal_id, text)``
    Добавляет комментарий тренера и возвращает запись комментария.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
DEFAULT_TIMEZONE = "Europe/Moscow"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("Дата должна быть в формате YYYY-MM-DD") from exc


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Дата и время должны быть в формате ISO 8601") from exc
    if result.tzinfo is None:
        raise ValueError("Дата и время должны содержать часовой пояс")
    return result.astimezone(timezone.utc)


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Неизвестный часовой пояс: {value}") from exc
    return value


def _number(
    value: Any,
    name: str,
    *,
    allow_none: bool = False,
    max_value: float | None = None,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} должно быть числом")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} должно быть числом") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} должно быть конечным числом")
    if result < 0:
        raise ValueError(f"{name} не может быть отрицательным")
    if max_value is not None and result > max_value:
        raise ValueError(f"{name} превышает допустимое значение {max_value:g}")
    return result


class NutritionStore:
    """SQLite API с проверкой доступа и короткими транзакциями."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        parent_existed = self.db_path.parent.exists()
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            self.db_path.parent.chmod(0o700)
        descriptor = os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.db_path.chmod(0o600)
        self._migrate()
        self._harden_permissions()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._harden_permissions()

    def _harden_permissions(self) -> None:
        for candidate in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if candidate.exists():
                candidate.chmod(0o600)

    def _migrate(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS photo_consents (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    version TEXT NOT NULL,
                    accepted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trainer_invites (
                    code TEXT PRIMARY KEY,
                    trainer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    uses INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trainer_clients (
                    trainer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    client_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    active INTEGER NOT NULL DEFAULT 1,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (trainer_user_id, client_user_id),
                    CHECK (trainer_user_id <> client_user_id)
                );

                CREATE TABLE IF NOT EXISTS nutrition_norms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    effective_from TEXT NOT NULL,
                    calories REAL,
                    protein_g REAL,
                    fat_g REAL,
                    carbs_g REAL,
                    water_ml INTEGER,
                    set_by_user_id INTEGER NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    UNIQUE (client_user_id, effective_from)
                );

                CREATE TABLE IF NOT EXISTS meals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    source TEXT NOT NULL CHECK (source IN ('photo', 'manual')),
                    photo_file_id TEXT,
                    eaten_at TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    meal_type TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'cancelled')),
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    confirmation_key TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE (client_user_id, confirmation_key)
                );

                CREATE INDEX IF NOT EXISTS idx_meals_client_date
                    ON meals(client_user_id, local_date, status);

                CREATE TABLE IF NOT EXISTS meal_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    weight_g REAL,
                    portion_text TEXT NOT NULL DEFAULT '',
                    calories REAL NOT NULL,
                    protein_g REAL NOT NULL,
                    fat_g REAL NOT NULL,
                    carbs_g REAL NOT NULL,
                    approximate INTEGER NOT NULL DEFAULT 1,
                    manually_edited INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS water_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    amount_ml INTEGER NOT NULL CHECK (amount_ml > 0),
                    logged_at TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (client_user_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_water_client_date
                    ON water_logs(client_user_id, local_date);

                CREATE TABLE IF NOT EXISTS trainer_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
                    trainer_user_id INTEGER NOT NULL REFERENCES users(id),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            row = db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Версия базы {row['version']} не поддерживается кодом {SCHEMA_VERSION}"
                )

    def backup(self, target_path: str | Path) -> Path:
        """Создает согласованный бэкап через SQLite backup API."""
        target = Path(target_path)
        parent_existed = target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            target.parent.chmod(0o700)
        with sqlite3.connect(self.db_path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        target.chmod(0o600)
        return target

    def ensure_user(
        self,
        *,
        telegram_id: int,
        display_name: str,
        timezone_name: str | None = None,
    ) -> dict[str, Any]:
        insert_timezone = _validate_timezone(timezone_name or DEFAULT_TIMEZONE)
        display_name = (display_name or "Пользователь").strip()[:200]
        now = _utc_now()
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO users(telegram_id, display_name, timezone, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = excluded.updated_at
                """,
                (int(telegram_id), display_name, insert_timezone, now, now),
            )
            row = db.execute("SELECT * FROM users WHERE telegram_id = ?", (int(telegram_id),)).fetchone()
            return dict(row)

    def set_timezone(self, *, telegram_id: int, timezone_name: str) -> dict[str, Any]:
        """Меняет часовой пояс только по явному действию пользователя."""
        timezone_name = _validate_timezone(timezone_name)
        user = self._require_user(telegram_id)
        with self._connection() as db:
            db.execute(
                "UPDATE users SET timezone = ?, updated_at = ? WHERE id = ?",
                (timezone_name, _utc_now(), user["id"]),
            )
            row = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            self._audit(db, user["id"], "set_timezone", "user", user["id"], {"timezone": timezone_name})
            return dict(row)

    def get_user_by_telegram_id(self, telegram_id: int) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM users WHERE telegram_id = ?", (int(telegram_id),)).fetchone()
            return dict(row) if row else None

    def set_photo_consent(self, *, telegram_id: int, version: str = "nutrition-photo-v1") -> dict[str, Any]:
        user = self._require_user(telegram_id)
        accepted_at = _utc_now()
        with self._connection() as db:
            db.execute(
                """
                INSERT INTO photo_consents(user_id, version, accepted_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    version = excluded.version,
                    accepted_at = excluded.accepted_at
                """,
                (user["id"], version, accepted_at),
            )
        return {"version": version, "accepted_at": accepted_at}

    def has_photo_consent(self, *, telegram_id: int, version: str = "nutrition-photo-v1") -> bool:
        user = self._require_user(telegram_id)
        with self._connection() as db:
            row = db.execute(
                "SELECT 1 FROM photo_consents WHERE user_id = ? AND version = ?",
                (user["id"], version),
            ).fetchone()
            return row is not None

    def revoke_photo_consent(self, *, telegram_id: int) -> None:
        """Отзывает согласие на будущие AI-запросы с фото."""
        user = self._require_user(telegram_id)
        with self._connection() as db:
            db.execute("DELETE FROM photo_consents WHERE user_id = ?", (user["id"],))
            self._audit(db, user["id"], "revoke_photo_consent", "user", user["id"], {})

    def create_trainer_invite(
        self,
        *,
        trainer_telegram_id: int,
        valid_hours: int = 168,
        max_uses: int = 1,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        if valid_hours <= 0 or max_uses <= 0:
            raise ValueError("Срок и число использований должны быть положительными")
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(hours=valid_hours)
        for _ in range(5):
            code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()
            try:
                with self._connection() as db:
                    db.execute(
                        """
                        INSERT INTO trainer_invites(
                            code, trainer_user_id, expires_at, max_uses, uses, created_at
                        ) VALUES (?, ?, ?, ?, 0, ?)
                        """,
                        (
                            code,
                            trainer["id"],
                            expires_at.isoformat(timespec="seconds"),
                            max_uses,
                            created_at.isoformat(timespec="seconds"),
                        ),
                    )
                return {"code": code, "expires_at": expires_at.isoformat(timespec="seconds")}
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Не удалось создать уникальный код")

    def link_client_by_code(self, *, client_telegram_id: int, code: str) -> dict[str, Any]:
        client = self._require_user(client_telegram_id)
        normalized = code.strip().upper()
        now = datetime.now(timezone.utc)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            invite = db.execute(
                "SELECT * FROM trainer_invites WHERE code = ?", (normalized,)
            ).fetchone()
            if invite is None:
                raise ValueError("Код тренера не найден")
            if datetime.fromisoformat(invite["expires_at"]) <= now:
                raise ValueError("Срок действия кода тренера истек")
            if invite["uses"] >= invite["max_uses"]:
                raise ValueError("Код тренера уже использован")
            if invite["trainer_user_id"] == client["id"]:
                raise ValueError("Тренер не может привязать себя как клиента")
            reserved = db.execute(
                """
                UPDATE trainer_invites SET uses = uses + 1
                WHERE code = ? AND uses < max_uses AND expires_at > ?
                """,
                (normalized, now.isoformat(timespec="seconds")),
            )
            if reserved.rowcount != 1:
                raise ValueError("Код тренера уже использован или просрочен")
            db.execute(
                """
                UPDATE trainer_clients SET active = 0
                WHERE client_user_id = ? AND trainer_user_id <> ? AND active = 1
                """,
                (client["id"], invite["trainer_user_id"]),
            )
            db.execute(
                """
                INSERT INTO trainer_clients(trainer_user_id, client_user_id, active, linked_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(trainer_user_id, client_user_id) DO UPDATE SET active = 1
                """,
                (invite["trainer_user_id"], client["id"], _utc_now()),
            )
            trainer = db.execute("SELECT * FROM users WHERE id = ?", (invite["trainer_user_id"],)).fetchone()
            self._audit(db, client["id"], "link", "trainer_client", client["id"], {})
            return {
                "client_id": client["id"],
                "trainer_id": trainer["id"],
                "trainer_display_name": trainer["display_name"],
            }

    def unlink_client(self, *, trainer_telegram_id: int, client_id: int) -> None:
        """Отключает доступ тренера к клиенту."""
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            self._require_trainer_access(db, trainer["id"], client_id)
            db.execute(
                """
                UPDATE trainer_clients SET active = 0
                WHERE trainer_user_id = ? AND client_user_id = ?
                """,
                (trainer["id"], client_id),
            )
            self._audit(db, trainer["id"], "unlink", "trainer_client", client_id, {})

    def unlink_trainer(self, *, client_telegram_id: int) -> None:
        """Позволяет клиенту закрыть все действующие связи с тренерами."""
        client = self._require_user(client_telegram_id)
        with self._connection() as db:
            db.execute(
                "UPDATE trainer_clients SET active = 0 WHERE client_user_id = ? AND active = 1",
                (client["id"],),
            )
            self._audit(db, client["id"], "unlink", "trainer_client", client["id"], {})

    def create_meal_draft(
        self,
        *,
        client_telegram_id: int,
        source: str,
        eaten_at: str | datetime,
        meal_type: str,
        items: Iterable[dict[str, Any]],
        photo_file_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        if source not in {"photo", "manual"}:
            raise ValueError("Источник должен быть photo или manual")
        user = self._require_user(client_telegram_id)
        if source == "photo" and not self.has_photo_consent(telegram_id=client_telegram_id):
            raise PermissionError("Нет согласия на AI-анализ фото")
        eaten_utc = _parse_datetime(eaten_at)
        tz_name = user["timezone"]
        local_date = eaten_utc.astimezone(ZoneInfo(tz_name)).date().isoformat()
        prepared = self._validate_items(items, require_portion=False)
        if not prepared:
            raise ValueError("Нужен хотя бы один продукт или блюдо")
        now = _utc_now()
        with self._connection() as db:
            cursor = db.execute(
                """
                INSERT INTO meals(
                    client_user_id, source, photo_file_id, eaten_at, local_date, timezone,
                    meal_type, note, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    user["id"], source, photo_file_id,
                    eaten_utc.isoformat(timespec="seconds"), local_date, tz_name,
                    meal_type.strip() or "прием пищи", note.strip()[:1000], now, now,
                ),
            )
            meal_id = cursor.lastrowid
            self._insert_items(db, meal_id, prepared)
            created = self._meal_by_id(db, meal_id)
            self._audit(
                db, user["id"], "create_draft", "meal", meal_id,
                {"source": source, "after": created},
            )
            return created

    def replace_draft_items(
        self,
        *,
        client_telegram_id: int,
        meal_id: int,
        items: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        prepared = self._validate_items(items, require_portion=False)
        if not prepared:
            raise ValueError("Нужен хотя бы один продукт или блюдо")
        with self._connection() as db:
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] != "draft":
                raise ValueError("Изменять можно только черновик")
            before = self._meal_by_id(db, meal_id)
            db.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
            self._insert_items(db, meal_id, prepared, manually_edited=True)
            db.execute("UPDATE meals SET updated_at = ? WHERE id = ?", (_utc_now(), meal_id))
            after = self._meal_by_id(db, meal_id)
            self._audit(
                db, user["id"], "replace_items", "meal", meal_id,
                {"before": before, "after": after},
            )
            return after

    def confirm_meal(
        self,
        *,
        client_telegram_id: int,
        meal_id: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        if not idempotency_key.strip():
            raise ValueError("Нужен ключ идемпотентности")
        with self._connection() as db:
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] == "confirmed":
                return self._meal_by_id(db, meal_id)
            if meal["status"] == "cancelled":
                raise ValueError("Отмененный прием пищи нельзя подтвердить")
            items = db.execute("SELECT * FROM meal_items WHERE meal_id = ?", (meal_id,)).fetchall()
            self._validate_items([dict(item) for item in items], require_portion=True)
            try:
                db.execute(
                    """
                    UPDATE meals SET status = 'confirmed', confirmed_at = ?,
                        confirmation_key = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_utc_now(), idempotency_key, _utc_now(), meal_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Этот запрос подтверждения уже использован") from exc
            self._audit(db, user["id"], "confirm", "meal", meal_id, {})
            return self._meal_by_id(db, meal_id)

    def cancel_meal(self, *, client_telegram_id: int, meal_id: int) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] == "confirmed":
                raise ValueError("Подтвержденную запись можно исправить, но не отменить как черновик")
            db.execute(
                "UPDATE meals SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (_utc_now(), meal_id),
            )
            self._audit(db, user["id"], "cancel", "meal", meal_id, {})
            return self._meal_by_id(db, meal_id)

    def add_water(
        self,
        *,
        client_telegram_id: int,
        amount_ml: int,
        logged_at: str | datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        if isinstance(amount_ml, bool):
            raise ValueError("Объем воды должен быть целым числом")
        try:
            amount = int(amount_ml)
        except (TypeError, ValueError) as exc:
            raise ValueError("Объем воды должен быть целым числом") from exc
        if amount <= 0 or amount > 10000:
            raise ValueError("Объем воды должен быть от 1 до 10000 мл")
        logged_utc = _parse_datetime(logged_at)
        local_date = logged_utc.astimezone(ZoneInfo(user["timezone"])).date().isoformat()
        with self._connection() as db:
            existing = db.execute(
                "SELECT * FROM water_logs WHERE client_user_id = ? AND idempotency_key = ?",
                (user["id"], idempotency_key),
            ).fetchone()
            if existing:
                return dict(existing)
            cursor = db.execute(
                """
                INSERT INTO water_logs(
                    client_user_id, amount_ml, logged_at, local_date, timezone,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"], amount, logged_utc.isoformat(timespec="seconds"),
                    local_date, user["timezone"], idempotency_key, _utc_now(),
                ),
            )
            self._audit(db, user["id"], "add", "water", cursor.lastrowid, {"amount_ml": amount})
            row = db.execute("SELECT * FROM water_logs WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def get_own_day(self, *, client_telegram_id: int, local_date: str | date) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            return self._day_summary(db, user, _parse_date(local_date))

    def get_own_week(self, *, client_telegram_id: int, week_start: str | date) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            return self._week_summary(db, user, _parse_date(week_start))

    def list_trainer_clients(self, trainer_telegram_id: int) -> list[dict[str, Any]]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT u.id, u.telegram_id, u.display_name, u.timezone
                FROM trainer_clients tc
                JOIN users u ON u.id = tc.client_user_id
                WHERE tc.trainer_user_id = ? AND tc.active = 1
                ORDER BY lower(u.display_name), u.id
                """,
                (trainer["id"],),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_client_day(
        self,
        trainer_telegram_id: int,
        client_id: int,
        local_date: str | date,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            client = self._require_trainer_access(db, trainer["id"], client_id)
            return self._day_summary(db, client, _parse_date(local_date))

    def get_client_week(
        self,
        trainer_telegram_id: int,
        client_id: int,
        week_start: str | date,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            client = self._require_trainer_access(db, trainer["id"], client_id)
            return self._week_summary(db, client, _parse_date(week_start))

    def update_meal_as_trainer(
        self,
        trainer_telegram_id: int,
        meal_id: int,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        allowed = {"eaten_at", "meal_type", "note", "items"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Неподдерживаемые поля: {', '.join(sorted(unknown))}")
        with self._connection() as db:
            meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if meal is None:
                raise ValueError("Прием пищи не найден")
            client = self._require_trainer_access(db, trainer["id"], meal["client_user_id"])
            if meal["status"] != "confirmed":
                raise ValueError("Тренер может исправлять только подтвержденные записи")
            before = self._meal_by_id(db, meal_id)
            sql_updates: list[str] = []
            values: list[Any] = []
            if "eaten_at" in updates:
                eaten_utc = _parse_datetime(updates["eaten_at"])
                sql_updates.extend(["eaten_at = ?", "local_date = ?"])
                values.extend([
                    eaten_utc.isoformat(timespec="seconds"),
                    eaten_utc.astimezone(ZoneInfo(client["timezone"])).date().isoformat(),
                ])
            if "meal_type" in updates:
                sql_updates.append("meal_type = ?")
                values.append(str(updates["meal_type"]).strip()[:100])
            if "note" in updates:
                sql_updates.append("note = ?")
                values.append(str(updates["note"]).strip()[:1000])
            if sql_updates:
                sql_updates.append("updated_at = ?")
                values.append(_utc_now())
                values.append(meal_id)
                db.execute(f"UPDATE meals SET {', '.join(sql_updates)} WHERE id = ?", values)
            if "items" in updates:
                items = self._validate_items(updates["items"], require_portion=True)
                if not items:
                    raise ValueError("Нужен хотя бы один продукт или блюдо")
                db.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
                self._insert_items(db, meal_id, items, manually_edited=True)
            after = self._meal_by_id(db, meal_id)
            self._audit(
                db, trainer["id"], "trainer_update", "meal", meal_id,
                {"before": before, "after": after},
            )
            return after

    def set_norms(
        self,
        trainer_telegram_id: int,
        client_id: int,
        norms: dict[str, Any],
        effective_from: str | date,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        date_value = _parse_date(effective_from).isoformat()
        normalized = self._normalize_norms(norms)
        with self._connection() as db:
            self._require_trainer_access(db, trainer["id"], client_id)
            before = db.execute(
                "SELECT * FROM nutrition_norms WHERE client_user_id = ? AND effective_from = ?",
                (client_id, date_value),
            ).fetchone()
            db.execute(
                """
                INSERT INTO nutrition_norms(
                    client_user_id, effective_from, calories, protein_g, fat_g,
                    carbs_g, water_ml, set_by_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_user_id, effective_from) DO UPDATE SET
                    calories = excluded.calories,
                    protein_g = excluded.protein_g,
                    fat_g = excluded.fat_g,
                    carbs_g = excluded.carbs_g,
                    water_ml = excluded.water_ml,
                    set_by_user_id = excluded.set_by_user_id,
                    created_at = excluded.created_at
                """,
                (
                    client_id, date_value, normalized["calories"], normalized["protein_g"],
                    normalized["fat_g"], normalized["carbs_g"], normalized["water_ml"],
                    trainer["id"], _utc_now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM nutrition_norms WHERE client_user_id = ? AND effective_from = ?",
                (client_id, date_value),
            ).fetchone()
            self._audit(
                db, trainer["id"], "set_norms", "user", client_id,
                {"before": dict(before) if before else None, "after": dict(row)},
            )
            return dict(row)

    def add_comment(
        self,
        trainer_telegram_id: int,
        meal_id: int,
        text: str,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        value = text.strip()
        if not value:
            raise ValueError("Комментарий не может быть пустым")
        if len(value) > 2000:
            raise ValueError("Комментарий слишком длинный")
        with self._connection() as db:
            meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if meal is None:
                raise ValueError("Прием пищи не найден")
            self._require_trainer_access(db, trainer["id"], meal["client_user_id"])
            cursor = db.execute(
                """
                INSERT INTO trainer_comments(meal_id, trainer_user_id, text, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (meal_id, trainer["id"], value, _utc_now()),
            )
            self._audit(
                db, trainer["id"], "comment", "meal", meal_id,
                {"comment_id": cursor.lastrowid, "text": value},
            )
            row = db.execute("SELECT * FROM trainer_comments WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def get_meal_history(
        self,
        trainer_telegram_id: int,
        meal_id: int,
    ) -> list[dict[str, Any]]:
        """Возвращает журнал приема после проверки связи тренера с клиентом."""
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if meal is None:
                raise ValueError("Прием пищи не найден")
            self._require_trainer_access(db, trainer["id"], meal["client_user_id"])
            rows = db.execute(
                """
                SELECT id, actor_user_id, action, entity_type, entity_id,
                       details_json, created_at
                FROM audit_log
                WHERE entity_type = 'meal' AND entity_id = ?
                ORDER BY id
                """,
                (meal_id,),
            ).fetchall()
            result = []
            for row in rows:
                event = dict(row)
                event["details"] = json.loads(event.pop("details_json"))
                result.append(event)
            return result

    def _require_user(self, telegram_id: int) -> dict[str, Any]:
        user = self.get_user_by_telegram_id(telegram_id)
        if user is None:
            raise ValueError("Пользователь не зарегистрирован в дневнике питания")
        return user

    @staticmethod
    def _require_trainer_access(
        db: sqlite3.Connection,
        trainer_user_id: int,
        client_user_id: int,
    ) -> dict[str, Any]:
        row = db.execute(
            """
            SELECT u.* FROM trainer_clients tc
            JOIN users u ON u.id = tc.client_user_id
            WHERE tc.trainer_user_id = ? AND tc.client_user_id = ? AND tc.active = 1
            """,
            (trainer_user_id, client_user_id),
        ).fetchone()
        if row is None:
            raise PermissionError("Клиент не привязан к этому тренеру")
        return dict(row)

    @staticmethod
    def _require_owned_meal(
        db: sqlite3.Connection,
        client_user_id: int,
        meal_id: int,
    ) -> sqlite3.Row:
        meal = db.execute(
            "SELECT * FROM meals WHERE id = ? AND client_user_id = ?",
            (meal_id, client_user_id),
        ).fetchone()
        if meal is None:
            raise PermissionError("Прием пищи принадлежит другому пользователю или не найден")
        return meal

    @staticmethod
    def _validate_items(
        items: Iterable[dict[str, Any]],
        *,
        require_portion: bool,
    ) -> list[dict[str, Any]]:
        prepared = []
        for raw in items:
            name = str(raw.get("name", "")).strip()[:200]
            if not name:
                raise ValueError("У каждого блюда должно быть название")
            weight = _number(raw.get("weight_g"), "Масса", allow_none=True, max_value=100000)
            portion_text = str(raw.get("portion_text", "")).strip()[:200]
            approximate = bool(raw.get("approximate", weight is None))
            if require_portion and not ((weight is not None and weight > 0) or portion_text):
                raise ValueError(f"Укажите массу или описание порции для блюда: {name}")
            prepared.append(
                {
                    "name": name,
                    "weight_g": weight,
                    "portion_text": portion_text,
                    "calories": _number(raw.get("calories", 0), "Калории", max_value=100000),
                    "protein_g": _number(raw.get("protein_g", raw.get("protein", 0)), "Белки", max_value=10000),
                    "fat_g": _number(raw.get("fat_g", raw.get("fat", 0)), "Жиры", max_value=10000),
                    "carbs_g": _number(raw.get("carbs_g", raw.get("carbs", 0)), "Углеводы", max_value=10000),
                    "approximate": approximate,
                }
            )
        return prepared

    @staticmethod
    def _insert_items(
        db: sqlite3.Connection,
        meal_id: int,
        items: Iterable[dict[str, Any]],
        manually_edited: bool = False,
    ) -> None:
        now = _utc_now()
        db.executemany(
            """
            INSERT INTO meal_items(
                meal_id, name, weight_g, portion_text, calories, protein_g,
                fat_g, carbs_g, approximate, manually_edited, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    meal_id, item["name"], item["weight_g"], item["portion_text"],
                    item["calories"], item["protein_g"], item["fat_g"], item["carbs_g"],
                    int(item["approximate"]), int(manually_edited), now,
                )
                for item in items
            ],
        )

    @staticmethod
    def _normalize_norms(norms: dict[str, Any]) -> dict[str, Any]:
        aliases = {
            "calories": "calories",
            "kcal": "calories",
            "protein_g": "protein_g",
            "protein": "protein_g",
            "fat_g": "fat_g",
            "fat": "fat_g",
            "carbs_g": "carbs_g",
            "carbs": "carbs_g",
            "water_ml": "water_ml",
            "water": "water_ml",
        }
        result = {"calories": None, "protein_g": None, "fat_g": None, "carbs_g": None, "water_ml": None}
        for key, value in norms.items():
            target = aliases.get(key)
            if target is None:
                raise ValueError(f"Неизвестное поле нормы: {key}")
            maximum = 20000 if target in {"calories", "water_ml"} else 2000
            result[target] = _number(value, target, allow_none=True, max_value=maximum)
        if all(value is None for value in result.values()):
            raise ValueError("Нужно указать хотя бы одну норму")
        if result["water_ml"] is not None:
            result["water_ml"] = int(result["water_ml"])
        return result

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int,
        details: dict[str, Any],
    ) -> None:
        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: clean(item)
                    for key, item in value.items()
                    if key not in {"photo_file_id", "confirmation_key"}
                }
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        safe_details = clean(details)
        db.execute(
            """
            INSERT INTO audit_log(actor_user_id, action, entity_type, entity_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (actor_user_id, action, entity_type, entity_id, json.dumps(safe_details, ensure_ascii=False), _utc_now()),
        )

    @staticmethod
    def _meal_by_id(db: sqlite3.Connection, meal_id: int) -> dict[str, Any]:
        meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
        if meal is None:
            raise ValueError("Прием пищи не найден")
        result = dict(meal)
        result["items"] = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM meal_items WHERE meal_id = ? ORDER BY id", (meal_id,)
            ).fetchall()
        ]
        result["comments"] = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM trainer_comments WHERE meal_id = ? ORDER BY id", (meal_id,)
            ).fetchall()
        ]
        return result

    @staticmethod
    def _effective_norms(
        db: sqlite3.Connection,
        client_user_id: int,
        local_date: date,
    ) -> dict[str, Any] | None:
        row = db.execute(
            """
            SELECT * FROM nutrition_norms
            WHERE client_user_id = ? AND effective_from <= ?
            ORDER BY effective_from DESC, id DESC LIMIT 1
            """,
            (client_user_id, local_date.isoformat()),
        ).fetchone()
        return dict(row) if row else None

    def _day_summary(
        self,
        db: sqlite3.Connection,
        user: dict[str, Any],
        local_date: date,
    ) -> dict[str, Any]:
        meals = [
            self._meal_by_id(db, row["id"])
            for row in db.execute(
                """
                SELECT id FROM meals
                WHERE client_user_id = ? AND local_date = ? AND status = 'confirmed'
                ORDER BY eaten_at, id
                """,
                (user["id"], local_date.isoformat()),
            ).fetchall()
        ]
        totals = {"calories": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
        for meal in meals:
            for item in meal["items"]:
                for field in totals:
                    totals[field] += float(item[field] or 0)
        water = db.execute(
            """
            SELECT COALESCE(SUM(amount_ml), 0) AS total
            FROM water_logs WHERE client_user_id = ? AND local_date = ?
            """,
            (user["id"], local_date.isoformat()),
        ).fetchone()["total"]
        client = {key: user[key] for key in ("id", "telegram_id", "display_name", "timezone")}
        return {
            "client": client,
            "date": local_date.isoformat(),
            "norms": self._effective_norms(db, user["id"], local_date),
            "meals": meals,
            "water_ml": int(water or 0),
            "totals": {key: round(value, 2) for key, value in totals.items()},
        }

    def _week_summary(
        self,
        db: sqlite3.Connection,
        user: dict[str, Any],
        week_start: date,
    ) -> dict[str, Any]:
        days = [self._day_summary(db, user, week_start + timedelta(days=offset)) for offset in range(7)]
        averages = {
            field: round(sum(day["totals"][field] for day in days) / 7, 2)
            for field in ("calories", "protein_g", "fat_g", "carbs_g")
        }
        averages["water_ml"] = round(sum(day["water_ml"] for day in days) / 7, 2)
        return {
            "client": days[0]["client"],
            "week_start": week_start.isoformat(),
            "days": days,
            "averages": averages,
        }
