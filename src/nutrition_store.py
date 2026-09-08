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
import hashlib
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 4
DEFAULT_TIMEZONE = "Europe/Moscow"
_UNSET = object()


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


def _local_time(value: Any, name: str, *, allow_none: bool = False) -> str | None:
    if value in (None, "") and allow_none:
        return None
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"{name} должно быть в формате ЧЧ:ММ") from exc
    return parsed.strftime("%H:%M")


def _local_times(value: Any, name: str, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} должно быть списком времени")
    result = sorted({_local_time(item, name) for item in value})
    if len(result) > limit:
        raise ValueError(f"{name}: можно указать не больше {limit}")
    return result


def _payload_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} должно быть целым числом от {minimum} до {maximum}")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError
        result = int(number)
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} должно быть целым числом от {minimum} до {maximum}"
        ) from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} должно быть от {minimum} до {maximum}")
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
                try:
                    candidate.chmod(0o600)
                except FileNotFoundError:
                    # WAL/SHM может исчезнуть между exists() и chmod() при закрытии
                    # последнего параллельного соединения.
                    pass

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
                    telegram_username TEXT,
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
                    expires_at TEXT,
                    max_uses INTEGER,
                    uses INTEGER NOT NULL DEFAULT 0,
                    invite_kind TEXT NOT NULL DEFAULT 'one_time',
                    revoked_at TEXT,
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
                    hunger_level INTEGER CHECK (hunger_level BETWEEN 1 AND 10),
                    mood TEXT CHECK (mood IN ('great', 'good', 'neutral', 'low', 'stressed')),
                    status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed', 'cancelled')),
                    version INTEGER NOT NULL DEFAULT 1,
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
                    calculation_method TEXT NOT NULL DEFAULT 'legacy',
                    reference_fdc_id TEXT,
                    reference_source TEXT,
                    reference_version TEXT,
                    reference_url TEXT,
                    reference_description TEXT,
                    reference_preparation TEXT,
                    reference_kcal_per_100g REAL,
                    reference_protein_per_100g REAL,
                    reference_fat_per_100g REAL,
                    reference_carbs_per_100g REAL,
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
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
                    version INTEGER NOT NULL DEFAULT 1,
                    cancelled_at TEXT,
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
                db.execute("BEGIN IMMEDIATE")
                self._create_v3_objects(db)
                self._create_v4_objects(db)
                db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] == 1:
                db.execute("BEGIN IMMEDIATE")
                invite_columns = {
                    column["name"] for column in db.execute("PRAGMA table_info(trainer_invites)")
                }
                if "invite_kind" not in invite_columns:
                    db.execute("ALTER TABLE trainer_invites RENAME TO trainer_invites_v1")
                    db.execute(
                        """
                        CREATE TABLE trainer_invites (
                            code TEXT PRIMARY KEY,
                            trainer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            expires_at TEXT,
                            max_uses INTEGER,
                            uses INTEGER NOT NULL DEFAULT 0,
                            invite_kind TEXT NOT NULL DEFAULT 'one_time',
                            revoked_at TEXT,
                            created_at TEXT NOT NULL
                        )
                        """
                    )
                    db.execute(
                        """
                        INSERT INTO trainer_invites(
                            code, trainer_user_id, expires_at, max_uses, uses,
                            invite_kind, revoked_at, created_at
                        )
                        SELECT code, trainer_user_id, expires_at, max_uses, uses,
                               'one_time', NULL, created_at
                        FROM trainer_invites_v1
                        """
                    )
                    db.execute("DROP TABLE trainer_invites_v1")
                existing = {
                    column["name"] for column in db.execute("PRAGMA table_info(meal_items)")
                }
                additions = {
                    "calculation_method": "TEXT NOT NULL DEFAULT 'legacy'",
                    "reference_fdc_id": "TEXT",
                    "reference_source": "TEXT",
                    "reference_version": "TEXT",
                    "reference_url": "TEXT",
                    "reference_description": "TEXT",
                    "reference_preparation": "TEXT",
                    "reference_kcal_per_100g": "REAL",
                    "reference_protein_per_100g": "REAL",
                    "reference_fat_per_100g": "REAL",
                    "reference_carbs_per_100g": "REAL",
                }
                for name, definition in additions.items():
                    if name not in existing:
                        db.execute(f"ALTER TABLE meal_items ADD COLUMN {name} {definition}")
                db.execute(
                    """
                    UPDATE meal_items SET calculation_method = 'ai'
                    WHERE approximate = 1 AND manually_edited = 0
                      AND meal_id IN (SELECT id FROM meals WHERE source = 'photo')
                    """
                )
                self._migrate_v2_objects(db)
                self._create_v4_objects(db)
                db.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            elif row["version"] == 2:
                db.execute("BEGIN IMMEDIATE")
                self._migrate_v2_objects(db)
                self._create_v4_objects(db)
                db.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            elif row["version"] == 3:
                db.execute("BEGIN IMMEDIATE")
                self._create_v4_objects(db)
                db.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Версия базы {row['version']} не поддерживается кодом {SCHEMA_VERSION}"
                )

    @staticmethod
    def _migrate_v2_objects(db: sqlite3.Connection) -> None:
        user_columns = {column["name"] for column in db.execute("PRAGMA table_info(users)")}
        if "telegram_username" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN telegram_username TEXT")
        meal_columns = {column["name"] for column in db.execute("PRAGMA table_info(meals)")}
        additions = {
            "hunger_level": "INTEGER CHECK (hunger_level BETWEEN 1 AND 10)",
            "mood": "TEXT CHECK (mood IN ('great', 'good', 'neutral', 'low', 'stressed'))",
            "version": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in additions.items():
            if name not in meal_columns:
                db.execute(f"ALTER TABLE meals ADD COLUMN {name} {definition}")
        water_columns = {column["name"] for column in db.execute("PRAGMA table_info(water_logs)")}
        water_additions = {
            "status": "TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled'))",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "cancelled_at": "TEXT",
        }
        for name, definition in water_additions.items():
            if name not in water_columns:
                db.execute(f"ALTER TABLE water_logs ADD COLUMN {name} {definition}")
        NutritionStore._create_v3_objects(db)

    @staticmethod
    def _create_v3_objects(db: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS nutrition_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                height_cm REAL CHECK (height_cm IS NULL OR (height_cm > 0 AND height_cm <= 300)),
                goal TEXT NOT NULL DEFAULT '',
                initial_weight_kg REAL CHECK (
                    initial_weight_kg IS NULL OR (initial_weight_kg > 0 AND initial_weight_kg <= 1000)
                ),
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS weight_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                weight_kg REAL NOT NULL CHECK (weight_kg > 0 AND weight_kg <= 1000),
                measured_at TEXT NOT NULL,
                local_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
                version INTEGER NOT NULL DEFAULT 1,
                cancelled_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (client_user_id, idempotency_key)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_weight_client_date ON weight_logs(client_user_id, local_date)",
            """
            CREATE TABLE IF NOT EXISTS nutrition_web_login_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nutrition_web_sessions (
                session_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nutrition_plan_previews (
                token_hash TEXT PRIMARY KEY,
                trainer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                client_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                rows_json TEXT NOT NULL,
                conflicts_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _create_v4_objects(db: sqlite3.Connection) -> None:
        profile_columns = {
            column["name"] for column in db.execute("PRAGMA table_info(nutrition_profiles)")
        }
        if "display_name_override" not in profile_columns:
            db.execute(
                "ALTER TABLE nutrition_profiles ADD COLUMN "
                "display_name_override INTEGER NOT NULL DEFAULT 0 "
                "CHECK (display_name_override IN (0, 1))"
            )
            # Existing v3 profiles may already contain a manually chosen name.
            # There is no old provenance flag, so preserving it is the safe migration.
            db.execute("UPDATE nutrition_profiles SET display_name_override=1")
        statements = (
            """
            CREATE TABLE IF NOT EXISTS nutrition_client_reminder_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                meal_enabled INTEGER NOT NULL DEFAULT 0 CHECK (meal_enabled IN (0, 1)),
                meal_times_json TEXT NOT NULL DEFAULT '[]',
                water_enabled INTEGER NOT NULL DEFAULT 0 CHECK (water_enabled IN (0, 1)),
                water_mode TEXT NOT NULL DEFAULT 'times' CHECK (water_mode IN ('times', 'interval')),
                water_times_json TEXT NOT NULL DEFAULT '[]',
                water_interval_minutes INTEGER,
                water_start_local TEXT,
                water_end_local TEXT,
                quiet_start TEXT,
                quiet_end TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                idempotency_key TEXT,
                payload_hash TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nutrition_trainer_reminder_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                digest_enabled INTEGER NOT NULL DEFAULT 0 CHECK (digest_enabled IN (0, 1)),
                digest_time TEXT NOT NULL DEFAULT '20:00',
                inactivity_days INTEGER NOT NULL DEFAULT 1 CHECK (inactivity_days BETWEEN 1 AND 7),
                calorie_comparison_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (calorie_comparison_enabled IN (0, 1)),
                over_plan_percent INTEGER NOT NULL DEFAULT 10
                    CHECK (over_plan_percent BETWEEN 0 AND 500),
                version INTEGER NOT NULL DEFAULT 1,
                idempotency_key TEXT,
                payload_hash TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nutrition_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                subject_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN ('meal', 'water', 'trainer_digest')),
                local_date TEXT NOT NULL,
                slot_key TEXT NOT NULL,
                preferences_scope TEXT NOT NULL CHECK (preferences_scope IN ('client', 'trainer')),
                preferences_version INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sending', 'sent', 'cancelled', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_at TEXT,
                sent_at TEXT,
                cancelled_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (recipient_user_id, kind, subject_user_id, local_date, slot_key)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_nutrition_outbox_due
            ON nutrition_notification_outbox(status, scheduled_for, id)
            """,
            """
            CREATE TABLE IF NOT EXISTS nutrition_mutation_dedup (
                actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (actor_user_id, action, idempotency_key)
            )
            """,
        )
        for statement in statements:
            db.execute(statement)

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
        telegram_username: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        insert_timezone = _validate_timezone(timezone_name or DEFAULT_TIMEZONE)
        display_name = (display_name or "Пользователь").strip()[:200]
        username_provided = telegram_username is not _UNSET
        username = None
        if telegram_username not in (_UNSET, None, ""):
            username = str(telegram_username).strip().lstrip("@").lower()
            if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
                username = None
        now = _utc_now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """
                SELECT u.*,COALESCE(p.display_name_override,0) AS display_name_override
                FROM users u LEFT JOIN nutrition_profiles p ON p.user_id=u.id
                WHERE u.telegram_id=?
                """,
                (int(telegram_id),),
            ).fetchone()
            if existing is None:
                db.execute(
                    """
                    INSERT INTO users(
                        telegram_id, display_name, telegram_username, timezone, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (int(telegram_id), display_name, username, insert_timezone, now, now),
                )
            else:
                effective_name = existing["display_name"] if existing["display_name_override"] else display_name
                effective_username = username if username_provided else existing["telegram_username"]
                db.execute(
                    """
                    UPDATE users SET display_name=?,telegram_username=?,updated_at=? WHERE id=?
                    """,
                    (effective_name, effective_username, now, existing["id"]),
                )
            row = db.execute("SELECT * FROM users WHERE telegram_id = ?", (int(telegram_id),)).fetchone()
            return dict(row)

    def set_timezone(self, *, telegram_id: int, timezone_name: str) -> dict[str, Any]:
        """Меняет часовой пояс только по явному действию пользователя."""
        timezone_name = _validate_timezone(timezone_name)
        user = self._require_user(telegram_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE users SET timezone = ?, updated_at = ? WHERE id = ?",
                (timezone_name, _utc_now(), user["id"]),
            )
            self._cancel_pending_notifications(
                db, recipient_user_id=user["id"], subject_user_id=user["id"]
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

    def get_or_create_trainer_code(self, *, trainer_telegram_id: int) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """
                SELECT * FROM trainer_invites
                WHERE trainer_user_id = ? AND invite_kind = 'reusable' AND revoked_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (trainer["id"],),
            ).fetchone()
            if current is not None:
                return dict(current)
            return self._insert_reusable_code(db, trainer["id"])

    def replace_trainer_code(self, *, trainer_telegram_id: int) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE trainer_invites SET revoked_at = ?
                WHERE trainer_user_id = ? AND invite_kind = 'reusable' AND revoked_at IS NULL
                """,
                (_utc_now(), trainer["id"]),
            )
            result = self._insert_reusable_code(db, trainer["id"])
            self._audit(db, trainer["id"], "replace_reusable_code", "user", trainer["id"], {})
            return result

    def link_client_by_code(self, *, client_telegram_id: int, code: str) -> dict[str, Any]:
        client = self._require_user(client_telegram_id)
        normalized = code.strip().upper()
        now = datetime.now(timezone.utc)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            invite = db.execute(
                "SELECT * FROM trainer_invites WHERE code = ? AND revoked_at IS NULL", (normalized,)
            ).fetchone()
            if invite is None:
                raise ValueError("Код тренера не найден")
            if invite["trainer_user_id"] == client["id"]:
                raise ValueError("Тренер не может привязать себя как клиента")
            existing_link = db.execute(
                """
                SELECT 1 FROM trainer_clients
                WHERE trainer_user_id = ? AND client_user_id = ? AND active = 1
                """,
                (invite["trainer_user_id"], client["id"]),
            ).fetchone()
            trainer = db.execute(
                "SELECT * FROM users WHERE id = ?", (invite["trainer_user_id"],)
            ).fetchone()
            if existing_link is not None:
                return {
                    "client_id": client["id"],
                    "trainer_id": trainer["id"],
                    "trainer_display_name": trainer["display_name"],
                }
            if invite["invite_kind"] == "one_time":
                if not invite["expires_at"] or datetime.fromisoformat(invite["expires_at"]) <= now:
                    raise ValueError("Срок действия кода тренера истек")
                if invite["max_uses"] is None or invite["uses"] >= invite["max_uses"]:
                    raise ValueError("Код тренера уже использован")
                reserved = db.execute(
                    """
                    UPDATE trainer_invites SET uses = uses + 1
                    WHERE code = ? AND uses < max_uses AND expires_at > ? AND revoked_at IS NULL
                    """,
                    (normalized, now.isoformat(timespec="seconds")),
                )
            elif invite["invite_kind"] == "reusable":
                reserved = db.execute(
                    """
                    UPDATE trainer_invites SET uses = uses + 1
                    WHERE code = ? AND invite_kind = 'reusable' AND revoked_at IS NULL
                    """,
                    (normalized,),
                )
            else:
                raise ValueError("Тип кода тренера не поддерживается")
            if reserved.rowcount != 1:
                raise ValueError("Код тренера уже недействителен")
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
            self._audit(db, client["id"], "link", "trainer_client", client["id"], {})
            return {
                "client_id": client["id"],
                "trainer_id": trainer["id"],
                "trainer_display_name": trainer["display_name"],
            }

    @staticmethod
    def _insert_reusable_code(db: sqlite3.Connection, trainer_user_id: int) -> dict[str, Any]:
        for _ in range(8):
            code = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16].upper()
            try:
                db.execute(
                    """
                    INSERT INTO trainer_invites(
                        code, trainer_user_id, expires_at, max_uses, uses,
                        invite_kind, revoked_at, created_at
                    ) VALUES (?, ?, NULL, NULL, 0, 'reusable', NULL, ?)
                    """,
                    (code, trainer_user_id, _utc_now()),
                )
                row = db.execute("SELECT * FROM trainer_invites WHERE code = ?", (code,)).fetchone()
                return dict(row)
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Не удалось создать уникальный код")

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
        hunger_level: int | None = None,
        mood: str | None = None,
        idempotency_key: str | None = None,
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
        hunger, prepared_mood = self._normalize_meal_context(hunger_level, mood)
        key = self._validate_idempotency_key(idempotency_key) if idempotency_key is not None else None
        fingerprint = _payload_hash({
            "source": source,
            "photo_file_id": photo_file_id,
            "eaten_at": eaten_utc.isoformat(timespec="seconds"),
            "timezone": tz_name,
            "meal_type": meal_type.strip() or "прием пищи",
            "note": note.strip()[:1000],
            "hunger_level": hunger,
            "mood": prepared_mood,
            "items": prepared,
        })
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if key is not None:
                previous = db.execute(
                    """
                    SELECT * FROM nutrition_mutation_dedup
                    WHERE actor_user_id=? AND action='create_meal_draft' AND idempotency_key=?
                    """,
                    (user["id"], key),
                ).fetchone()
                if previous is not None:
                    if previous["payload_hash"] != fingerprint:
                        raise RuntimeError("idempotency_conflict")
                    return self._meal_by_id(db, previous["entity_id"])
            cursor = db.execute(
                """
                INSERT INTO meals(
                    client_user_id, source, photo_file_id, eaten_at, local_date, timezone,
                    meal_type, note, hunger_level, mood, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    user["id"], source, photo_file_id,
                    eaten_utc.isoformat(timespec="seconds"), local_date, tz_name,
                    meal_type.strip() or "прием пищи", note.strip()[:1000], hunger,
                    prepared_mood, now, now,
                ),
            )
            meal_id = cursor.lastrowid
            self._insert_items(db, meal_id, prepared)
            created = self._meal_by_id(db, meal_id)
            self._audit(
                db, user["id"], "create_draft", "meal", meal_id,
                {"source": source, "after": created},
            )
            if key is not None:
                db.execute(
                    """
                    INSERT INTO nutrition_mutation_dedup(
                        actor_user_id,action,idempotency_key,payload_hash,
                        entity_type,entity_id,created_at
                    ) VALUES (?,'create_meal_draft',?,?, 'meal',?,?)
                    """,
                    (user["id"], key, fingerprint, meal_id, now),
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
            db.execute("BEGIN IMMEDIATE")
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] != "draft":
                raise ValueError("Изменять можно только черновик")
            before = self._meal_by_id(db, meal_id)
            db.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
            self._insert_items(db, meal_id, prepared, manually_edited=True)
            db.execute(
                "UPDATE meals SET updated_at = ?, version = version + 1 WHERE id = ?",
                (_utc_now(), meal_id),
            )
            after = self._meal_by_id(db, meal_id)
            self._audit(
                db, user["id"], "replace_items", "meal", meal_id,
                {"before": before, "after": after},
            )
            return after

    def get_owned_draft(self, *, client_telegram_id: int, meal_id: int) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] != "draft":
                raise ValueError("Изменять можно только черновик")
            return self._meal_by_id(db, meal_id)

    def replace_draft_item_with_reference(
        self,
        *,
        client_telegram_id: int,
        meal_id: int,
        item_id: int,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        prepared = self._validate_items([item], require_portion=True)
        if prepared[0]["calculation_method"] != "reference":
            raise ValueError("Для замены нужна запись справочника")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] != "draft":
                raise ValueError("Изменять можно только черновик")
            current = db.execute(
                "SELECT id FROM meal_items WHERE id = ? AND meal_id = ?", (item_id, meal_id)
            ).fetchone()
            if current is None:
                raise PermissionError("Продукт принадлежит другому приему или не найден")
            self._update_item(db, item_id, prepared[0], manually_edited=False)
            db.execute(
                "UPDATE meals SET updated_at = ?, version = version + 1 WHERE id = ?",
                (_utc_now(), meal_id),
            )
            after = self._meal_by_id(db, meal_id)
            self._audit(
                db, user["id"], "replace_item_from_reference", "meal", meal_id,
                {"item_id": item_id, "after": after},
            )
            return after

    def update_reference_item_weight(
        self,
        *,
        client_telegram_id: int,
        meal_id: int,
        item_id: int,
        grams: float | str,
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] != "draft":
                raise ValueError("Изменять можно только черновик")
            row = db.execute(
                "SELECT * FROM meal_items WHERE id = ? AND meal_id = ?", (item_id, meal_id)
            ).fetchone()
            if row is None:
                raise PermissionError("Продукт принадлежит другому приему или не найден")
            if row["calculation_method"] != "reference":
                raise ValueError("Массу можно пересчитать только для позиции справочника")
            updated = dict(row)
            updated["weight_g"] = grams
            prepared = self._validate_items([updated], require_portion=True)[0]
            self._update_item(db, item_id, prepared, manually_edited=False)
            db.execute(
                "UPDATE meals SET updated_at = ?, version = version + 1 WHERE id = ?",
                (_utc_now(), meal_id),
            )
            after = self._meal_by_id(db, meal_id)
            self._audit(
                db, user["id"], "recalculate_reference_item", "meal", meal_id,
                {"item_id": item_id, "grams": prepared["weight_g"]},
            )
            return after

    def confirm_meal(
        self,
        *,
        client_telegram_id: int,
        meal_id: int,
        idempotency_key: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        if not idempotency_key.strip():
            raise ValueError("Нужен ключ идемпотентности")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] == "confirmed":
                if meal["confirmation_key"] != idempotency_key:
                    raise ValueError("Прием пищи уже подтвержден")
                return self._meal_by_id(db, meal_id)
            if meal["status"] == "cancelled":
                raise ValueError("Отмененный прием пищи нельзя подтвердить")
            if expected_version is not None and int(meal["version"]) != int(expected_version):
                raise RuntimeError("stale_version")
            items = db.execute("SELECT * FROM meal_items WHERE meal_id = ?", (meal_id,)).fetchall()
            self._validate_items([dict(item) for item in items], require_portion=True)
            try:
                db.execute(
                    """
                    UPDATE meals SET status = 'confirmed', confirmed_at = ?,
                        confirmation_key = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (_utc_now(), idempotency_key, _utc_now(), meal_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Этот запрос подтверждения уже использован") from exc
            self._audit(db, user["id"], "confirm", "meal", meal_id, {})
            return self._meal_by_id(db, meal_id)

    def cancel_meal(
        self,
        *,
        client_telegram_id: int,
        meal_id: int,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] == "cancelled":
                return self._meal_by_id(db, meal_id)
            if expected_version is not None and int(expected_version) != int(meal["version"]):
                raise RuntimeError("stale_version")
            previous_status = meal["status"]
            db.execute(
                "UPDATE meals SET status = 'cancelled', updated_at = ?, version = version + 1 WHERE id = ?",
                (_utc_now(), meal_id),
            )
            self._audit(
                db, user["id"], "cancel_confirmed" if previous_status == "confirmed" else "cancel_draft",
                "meal", meal_id, {"previous_status": previous_status},
            )
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
                SELECT u.id, u.telegram_id, u.telegram_username, u.display_name, u.timezone
                FROM trainer_clients tc
                JOIN users u ON u.id = tc.client_user_id
                WHERE tc.trainer_user_id = ? AND tc.active = 1
                ORDER BY lower(u.display_name), u.id
                """,
                (trainer["id"],),
            ).fetchall()
            result = []
            for row in rows:
                client = dict(row)
                local_today = datetime.now(ZoneInfo(client["timezone"])).date()
                day = self._day_summary(db, client, local_today)
                last_meal = db.execute(
                    "SELECT MAX(eaten_at) AS value FROM meals "
                    "WHERE client_user_id=? AND status='confirmed'",
                    (client["id"],),
                ).fetchone()["value"]
                last_water = db.execute(
                    "SELECT MAX(logged_at) AS value FROM water_logs "
                    "WHERE client_user_id=? AND status='active'",
                    (client["id"],),
                ).fetchone()["value"]
                last_weight = db.execute(
                    "SELECT MAX(measured_at) AS value FROM weight_logs "
                    "WHERE client_user_id=? AND status='active'",
                    (client["id"],),
                ).fetchone()["value"]
                norms = day["norms"]
                if norms is None:
                    norms_status = "missing"
                else:
                    values = [
                        norms[key] for key in
                        ("calories", "protein_g", "fat_g", "carbs_g", "water_ml")
                    ]
                    norms_status = "set" if all(value is not None for value in values) else "partial"
                result.append({
                    **client,
                    "local_today": local_today.isoformat(),
                    "last_activity_at": max(
                        [value for value in (last_meal, last_water, last_weight) if value],
                        default=None,
                    ),
                    "last_meal_at": last_meal,
                    "today": {
                        "date": local_today.isoformat(),
                        "totals": day["totals"],
                        "water_ml": day["water_ml"],
                        "weight_kg": day["weight_kg"],
                        "meal_count": len(day["meals"]),
                    },
                    "norms_status": norms_status,
                    "chat_url": f"/api/clients/{client['id']}/chat" if client.get("telegram_username") else None,
                    "can_open_chat": bool(client.get("telegram_username")),
                })
            return result

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

    def list_recent_client_meals(
        self,
        trainer_telegram_id: int,
        client_id: int,
        *,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        trainer = self._require_user(trainer_telegram_id)
        limit = max(1, min(int(limit), 30))
        with self._connection() as db:
            self._require_trainer_access(db, trainer["id"], client_id)
            rows = db.execute(
                """
                SELECT id FROM meals
                WHERE client_user_id = ? AND status = 'confirmed'
                ORDER BY eaten_at DESC, id DESC LIMIT ?
                """,
                (client_id, limit),
            ).fetchall()
            return [self._meal_by_id(db, row["id"]) for row in rows]

    def get_client_meal(self, trainer_telegram_id: int, meal_id: int) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if meal is None:
                raise ValueError("Прием пищи не найден")
            self._require_trainer_access(db, trainer["id"], meal["client_user_id"])
            if meal["status"] != "confirmed":
                raise ValueError("Прием пищи еще не подтвержден")
            return self._meal_by_id(db, meal_id)

    def update_meal_as_trainer(
        self,
        trainer_telegram_id: int,
        meal_id: int,
        updates: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        allowed = {"eaten_at", "meal_type", "note", "hunger_level", "mood", "items"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Неподдерживаемые поля: {', '.join(sorted(unknown))}")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if meal is None:
                raise ValueError("Прием пищи не найден")
            client = self._require_trainer_access(db, trainer["id"], meal["client_user_id"])
            if meal["status"] != "confirmed":
                raise ValueError("Тренер может исправлять только подтвержденные записи")
            if expected_version is not None and int(meal["version"]) != int(expected_version):
                raise RuntimeError("stale_version")
            before = self._meal_by_id(db, meal_id)
            sql_updates: list[str] = []
            values: list[Any] = []
            if "eaten_at" in updates:
                eaten_utc = _parse_datetime(updates["eaten_at"])
                sql_updates.extend(["eaten_at = ?", "local_date = ?", "timezone = ?"])
                values.extend([
                    eaten_utc.isoformat(timespec="seconds"),
                    eaten_utc.astimezone(ZoneInfo(client["timezone"])).date().isoformat(),
                    client["timezone"],
                ])
            if "meal_type" in updates:
                sql_updates.append("meal_type = ?")
                values.append(str(updates["meal_type"]).strip()[:100])
            if "note" in updates:
                sql_updates.append("note = ?")
                values.append(str(updates["note"]).strip()[:1000])
            if "hunger_level" in updates or "mood" in updates:
                hunger, mood = self._normalize_meal_context(
                    updates.get("hunger_level", meal["hunger_level"]),
                    updates.get("mood", meal["mood"]),
                )
                if "hunger_level" in updates:
                    sql_updates.append("hunger_level = ?")
                    values.append(hunger)
                if "mood" in updates:
                    sql_updates.append("mood = ?")
                    values.append(mood)
            if sql_updates:
                sql_updates.append("updated_at = ?")
                values.append(_utc_now())
                sql_updates.append("version = version + 1")
                values.append(meal_id)
                db.execute(f"UPDATE meals SET {', '.join(sql_updates)} WHERE id = ?", values)
            if "items" in updates:
                items = self._validate_items(updates["items"], require_portion=True)
                if not items:
                    raise ValueError("Нужен хотя бы один продукт или блюдо")
                db.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
                self._insert_items(db, meal_id, items, manually_edited=True)
                if not sql_updates:
                    db.execute(
                        "UPDATE meals SET updated_at = ?, version = version + 1 WHERE id = ?",
                        (_utc_now(), meal_id),
                    )
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
            db.execute("BEGIN IMMEDIATE")
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

    def set_norms_bulk(
        self,
        trainer_telegram_id: int,
        client_id: int,
        rows: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Атомарно добавляет или заменяет нормы на явно указанные даты."""
        trainer = self._require_user(trainer_telegram_id)
        prepared: list[tuple[str, dict[str, Any]]] = []
        seen_dates: set[str] = set()
        for raw in rows:
            date_value = _parse_date(raw.get("effective_from", "")).isoformat()
            if date_value in seen_dates:
                raise ValueError(f"Дата {date_value} указана несколько раз")
            seen_dates.add(date_value)
            values = {key: value for key, value in raw.items() if key != "effective_from"}
            prepared.append((date_value, self._normalize_norms(values)))
        if not prepared:
            raise ValueError("План норм не содержит строк")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_trainer_access(db, trainer["id"], client_id)
            result = []
            for date_value, normalized in sorted(prepared):
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
                        client_id, date_value, normalized["calories"],
                        normalized["protein_g"], normalized["fat_g"],
                        normalized["carbs_g"], normalized["water_ml"],
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
                result.append(dict(row))
            return result

    def add_comment(
        self,
        trainer_telegram_id: int,
        meal_id: int,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        value = text.strip()
        if not value:
            raise ValueError("Комментарий не может быть пустым")
        if len(value) > 2000:
            raise ValueError("Комментарий слишком длинный")
        key = self._validate_idempotency_key(idempotency_key) if idempotency_key is not None else None
        fingerprint = _payload_hash({"meal_id": int(meal_id), "text": value})
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            meal = db.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if meal is None:
                raise ValueError("Прием пищи не найден")
            self._require_trainer_access(db, trainer["id"], meal["client_user_id"])
            if key is not None:
                previous = db.execute(
                    """
                    SELECT * FROM nutrition_mutation_dedup
                    WHERE actor_user_id=? AND action='add_comment' AND idempotency_key=?
                    """,
                    (trainer["id"], key),
                ).fetchone()
                if previous is not None:
                    if previous["payload_hash"] != fingerprint:
                        raise RuntimeError("idempotency_conflict")
                    row = db.execute(
                        "SELECT * FROM trainer_comments WHERE id=?", (previous["entity_id"],)
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("idempotency_conflict")
                    return dict(row)
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
            if key is not None:
                db.execute(
                    """
                    INSERT INTO nutrition_mutation_dedup(
                        actor_user_id,action,idempotency_key,payload_hash,
                        entity_type,entity_id,created_at
                    ) VALUES (?,'add_comment',?,?,'comment',?,?)
                    """,
                    (trainer["id"], key, fingerprint, cursor.lastrowid, _utc_now()),
                )
            row = db.execute("SELECT * FROM trainer_comments WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def get_profile(self, *, telegram_id: int) -> dict[str, Any]:
        user = self._require_user(telegram_id)
        with self._connection() as db:
            profile = db.execute(
                "SELECT * FROM nutrition_profiles WHERE user_id = ?", (user["id"],)
            ).fetchone()
            return {
                "user_id": user["id"],
                "display_name": user["display_name"],
                "timezone": user["timezone"],
                "height_cm": profile["height_cm"] if profile else None,
                "goal": profile["goal"] if profile else "",
                "initial_weight_kg": profile["initial_weight_kg"] if profile else None,
                "version": int(profile["version"]) if profile else 0,
                "updated_at": profile["updated_at"] if profile else user["updated_at"],
            }

    def update_profile(
        self,
        *,
        telegram_id: int,
        updates: dict[str, Any],
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        allowed = {"display_name", "timezone", "height_cm", "goal", "initial_weight_kg"}
        if not updates or set(updates) - allowed:
            raise ValueError("Профиль не содержит поддерживаемых изменений")
        user = self._require_user(telegram_id)
        display_name = None
        if "display_name" in updates:
            display_name = str(updates["display_name"] or "").strip()
            if not display_name or len(display_name) > 200:
                raise ValueError("Имя должно содержать от 1 до 200 символов")
        timezone_name = None
        if "timezone" in updates:
            timezone_name = _validate_timezone(str(updates["timezone"]))
        height_cm = None
        if "height_cm" in updates:
            height_cm = _number(updates["height_cm"], "Рост", allow_none=True, max_value=300)
            if height_cm is not None and height_cm <= 0:
                raise ValueError("Рост должен быть больше нуля")
        initial_weight = None
        if "initial_weight_kg" in updates:
            initial_weight = _number(
                updates["initial_weight_kg"], "Начальный вес", allow_none=True, max_value=1000
            )
            if initial_weight is not None and initial_weight <= 0:
                raise ValueError("Начальный вес должен быть больше нуля")
        goal = None
        if "goal" in updates:
            goal = str(updates["goal"] or "").strip()
            if len(goal) > 1000:
                raise ValueError("Описание цели слишком длинное")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM nutrition_profiles WHERE user_id = ?", (user["id"],)
            ).fetchone()
            current_version = int(current["version"]) if current else 0
            if expected_version is not None and int(expected_version) != current_version:
                raise RuntimeError("stale_version")
            if display_name is not None or timezone_name is not None:
                db.execute(
                    "UPDATE users SET display_name = COALESCE(?, display_name), "
                    "timezone = COALESCE(?, timezone), updated_at = ? WHERE id = ?",
                    (display_name, timezone_name, _utc_now(), user["id"]),
                )
            if timezone_name is not None:
                self._cancel_pending_notifications(
                    db, recipient_user_id=user["id"], subject_user_id=user["id"]
                )
            before = dict(current) if current else None
            merged = {
                "height_cm": current["height_cm"] if current else None,
                "goal": current["goal"] if current else "",
                "initial_weight_kg": current["initial_weight_kg"] if current else None,
                "display_name_override": int(current["display_name_override"]) if current else 0,
            }
            if "height_cm" in updates:
                merged["height_cm"] = height_cm
            if "goal" in updates:
                merged["goal"] = goal
            if "initial_weight_kg" in updates:
                merged["initial_weight_kg"] = initial_weight
            if "display_name" in updates:
                merged["display_name_override"] = 1
            next_version = current_version + 1
            now = _utc_now()
            db.execute(
                """
                INSERT INTO nutrition_profiles(
                    user_id, height_cm, goal, initial_weight_kg,
                    display_name_override, version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    height_cm=excluded.height_cm, goal=excluded.goal,
                    initial_weight_kg=excluded.initial_weight_kg,
                    display_name_override=excluded.display_name_override,
                    version=excluded.version, updated_at=excluded.updated_at
                """,
                (
                    user["id"], merged["height_cm"], merged["goal"],
                    merged["initial_weight_kg"], merged["display_name_override"],
                    next_version, now,
                ),
            )
            row = db.execute(
                """
                SELECT u.id AS user_id, u.display_name, u.timezone,
                       p.height_cm, p.goal, p.initial_weight_kg, p.version, p.updated_at
                FROM users u JOIN nutrition_profiles p ON p.user_id=u.id WHERE u.id=?
                """,
                (user["id"],),
            ).fetchone()
            self._audit(
                db, user["id"], "update_profile", "user", user["id"],
                {"before": before, "after": dict(row)},
            )
            return dict(row)

    def get_client_reminder_preferences(self, *, telegram_id: int) -> dict[str, Any]:
        user = self._require_user(telegram_id)
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM nutrition_client_reminder_preferences WHERE user_id=?",
                (user["id"],),
            ).fetchone()
            return self._client_reminder_payload(user, row)

    def update_client_reminder_preferences(
        self,
        *,
        telegram_id: int,
        updates: dict[str, Any],
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        user = self._require_user(telegram_id)
        key = self._validate_idempotency_key(idempotency_key)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM nutrition_client_reminder_preferences WHERE user_id=?",
                (user["id"],),
            ).fetchone()
            current_payload = self._client_reminder_payload(user, current)
            merged = self._normalize_client_reminders(current_payload, updates)
            fingerprint = _payload_hash(merged)
            if current is not None and current["idempotency_key"] == key:
                if current["payload_hash"] != fingerprint:
                    raise RuntimeError("idempotency_conflict")
                return current_payload
            if int(expected_version) != int(current_payload["version"]):
                raise RuntimeError("stale_version")
            next_version = int(current_payload["version"]) + 1
            now = _utc_now()
            meal = merged["meal"]
            water = merged["water"]
            quiet = merged["quiet_hours"]
            db.execute(
                """
                INSERT INTO nutrition_client_reminder_preferences(
                    user_id, enabled, meal_enabled, meal_times_json,
                    water_enabled, water_mode, water_times_json,
                    water_interval_minutes, water_start_local, water_end_local,
                    quiet_start, quiet_end, version, idempotency_key, payload_hash, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled=excluded.enabled, meal_enabled=excluded.meal_enabled,
                    meal_times_json=excluded.meal_times_json,
                    water_enabled=excluded.water_enabled, water_mode=excluded.water_mode,
                    water_times_json=excluded.water_times_json,
                    water_interval_minutes=excluded.water_interval_minutes,
                    water_start_local=excluded.water_start_local,
                    water_end_local=excluded.water_end_local,
                    quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end,
                    version=excluded.version, idempotency_key=excluded.idempotency_key,
                    payload_hash=excluded.payload_hash, updated_at=excluded.updated_at
                """,
                (
                    user["id"], int(merged["enabled"]), int(meal["enabled"]),
                    json.dumps(meal["times"]), int(water["enabled"]), water["mode"],
                    json.dumps(water["times"]), water["interval_minutes"],
                    water["start_local"], water["end_local"], quiet["start"], quiet["end"],
                    next_version, key, fingerprint, now,
                ),
            )
            self._cancel_pending_notifications(db, recipient_user_id=user["id"], subject_user_id=user["id"])
            self._audit(
                db, user["id"], "update_client_reminders", "user", user["id"],
                {"version": next_version, "enabled": merged["enabled"]},
            )
            row = db.execute(
                "SELECT * FROM nutrition_client_reminder_preferences WHERE user_id=?",
                (user["id"],),
            ).fetchone()
            return self._client_reminder_payload(user, row)

    def get_trainer_reminder_preferences(self, *, trainer_telegram_id: int) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM nutrition_trainer_reminder_preferences WHERE user_id=?",
                (trainer["id"],),
            ).fetchone()
            return self._trainer_reminder_payload(trainer, row)

    def update_trainer_reminder_preferences(
        self,
        *,
        trainer_telegram_id: int,
        updates: dict[str, Any],
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        key = self._validate_idempotency_key(idempotency_key)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM nutrition_trainer_reminder_preferences WHERE user_id=?",
                (trainer["id"],),
            ).fetchone()
            current_payload = self._trainer_reminder_payload(trainer, current)
            merged = self._normalize_trainer_reminders(current_payload, updates)
            fingerprint = _payload_hash(merged)
            if current is not None and current["idempotency_key"] == key:
                if current["payload_hash"] != fingerprint:
                    raise RuntimeError("idempotency_conflict")
                return current_payload
            if int(expected_version) != int(current_payload["version"]):
                raise RuntimeError("stale_version")
            next_version = int(current_payload["version"]) + 1
            now = _utc_now()
            digest = merged["daily_digest"]
            db.execute(
                """
                INSERT INTO nutrition_trainer_reminder_preferences(
                    user_id, enabled, digest_enabled, digest_time, inactivity_days,
                    calorie_comparison_enabled, over_plan_percent, version,
                    idempotency_key, payload_hash, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled=excluded.enabled, digest_enabled=excluded.digest_enabled,
                    digest_time=excluded.digest_time, inactivity_days=excluded.inactivity_days,
                    calorie_comparison_enabled=excluded.calorie_comparison_enabled,
                    over_plan_percent=excluded.over_plan_percent, version=excluded.version,
                    idempotency_key=excluded.idempotency_key,
                    payload_hash=excluded.payload_hash, updated_at=excluded.updated_at
                """,
                (
                    trainer["id"], int(merged["enabled"]), int(digest["enabled"]),
                    digest["time"], digest["inactivity_days"],
                    int(digest["calorie_comparison_enabled"]), digest["over_plan_percent"],
                    next_version, key, fingerprint, now,
                ),
            )
            self._cancel_pending_notifications(db, recipient_user_id=trainer["id"])
            self._audit(
                db, trainer["id"], "update_trainer_reminders", "user", trainer["id"],
                {"version": next_version, "enabled": merged["enabled"]},
            )
            row = db.execute(
                "SELECT * FROM nutrition_trainer_reminder_preferences WHERE user_id=?",
                (trainer["id"],),
            ).fetchone()
            return self._trainer_reminder_payload(trainer, row)

    def create_browser_login_token(
        self,
        *,
        telegram_id: int,
        ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        user = self._require_user(telegram_id)
        if ttl_seconds < 60 or ttl_seconds > 3600:
            raise ValueError("Недопустимый срок ссылки")
        raw = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM nutrition_web_login_tokens WHERE user_id=? AND consumed_at IS NULL",
                (user["id"],),
            )
            db.execute(
                "INSERT INTO nutrition_web_login_tokens(token_hash,user_id,expires_at,created_at) "
                "VALUES (?,?,?,?)",
                (
                    self._token_hash(raw), user["id"], expires.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
        return {"token": raw, "expires_at": expires.isoformat(timespec="seconds")}

    def start_browser_session(
        self,
        *,
        telegram_id: int,
        session_token: str,
        csrf_token: str,
        ttl_seconds: int = 43_200,
    ) -> dict[str, Any]:
        user = self._require_user(telegram_id)
        return self._insert_browser_session(
            user_id=user["id"], session_token=session_token, csrf_token=csrf_token,
            ttl_seconds=ttl_seconds,
        )

    def exchange_browser_login_token(
        self,
        *,
        login_token: str,
        session_token: str,
        csrf_token: str,
        ttl_seconds: int = 43_200,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM nutrition_web_login_tokens WHERE token_hash=?",
                (self._token_hash(login_token),),
            ).fetchone()
            if row is None or row["consumed_at"] is not None or _parse_datetime(row["expires_at"]) <= now:
                raise PermissionError("Ссылка недействительна или уже использована")
            db.execute(
                "UPDATE nutrition_web_login_tokens SET consumed_at=? WHERE token_hash=?",
                (now.isoformat(timespec="seconds"), row["token_hash"]),
            )
            result = self._insert_browser_session(
                user_id=row["user_id"], session_token=session_token,
                csrf_token=csrf_token, ttl_seconds=ttl_seconds, db=db,
            )
            return result

    def _insert_browser_session(
        self,
        *,
        user_id: int,
        session_token: str,
        csrf_token: str,
        ttl_seconds: int,
        db: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if ttl_seconds < 300 or ttl_seconds > 86_400:
            raise ValueError("Недопустимый срок сессии")
        if len(session_token) < 32 or len(csrf_token) < 32:
            raise ValueError("Недостаточная длина токена")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)

        def insert(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute(
                """
                INSERT INTO nutrition_web_sessions(
                    session_hash,user_id,csrf_hash,expires_at,created_at,last_seen_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    self._token_hash(session_token), user_id, self._token_hash(csrf_token),
                    expires.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
            user = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return {"user": dict(user), "expires_at": expires.isoformat(timespec="seconds")}

        if db is not None:
            return insert(db)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return insert(connection)

    def authenticate_browser_session(self, *, session_token: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._connection() as db:
            row = db.execute(
                """
                SELECT s.*, u.telegram_id, u.display_name, u.timezone
                FROM nutrition_web_sessions s JOIN users u ON u.id=s.user_id
                WHERE s.session_hash=? AND s.revoked_at IS NULL
                """,
                (self._token_hash(session_token),),
            ).fetchone()
            if row is None or _parse_datetime(row["expires_at"]) <= now:
                raise PermissionError("Сессия истекла")
            return dict(row)

    def validate_browser_csrf(self, *, session_token: str, csrf_token: str) -> bool:
        session = self.authenticate_browser_session(session_token=session_token)
        return secrets.compare_digest(session["csrf_hash"], self._token_hash(csrf_token))

    def revoke_browser_session(self, *, session_token: str) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE nutrition_web_sessions SET revoked_at=? WHERE session_hash=?",
                (_utc_now(), self._token_hash(session_token)),
            )

    def add_weight(
        self,
        *,
        client_telegram_id: int,
        weight_kg: Any,
        measured_at: str | datetime,
        idempotency_key: str,
        note: str = "",
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        weight = _number(weight_kg, "Вес", max_value=1000)
        if weight is None or weight <= 0:
            raise ValueError("Вес должен быть больше нуля")
        if not idempotency_key.strip():
            raise ValueError("Нужен ключ идемпотентности")
        measured = _parse_datetime(measured_at)
        local_date = measured.astimezone(ZoneInfo(user["timezone"])).date().isoformat()
        clean_note = str(note or "").strip()
        if len(clean_note) > 500:
            raise ValueError("Заметка о весе слишком длинная")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM weight_logs WHERE client_user_id=? AND idempotency_key=?",
                (user["id"], idempotency_key),
            ).fetchone()
            if existing:
                return dict(existing)
            cursor = db.execute(
                """
                INSERT INTO weight_logs(
                    client_user_id,weight_kg,measured_at,local_date,timezone,note,
                    idempotency_key,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    user["id"], weight, measured.isoformat(timespec="seconds"), local_date,
                    user["timezone"], clean_note, idempotency_key, _utc_now(),
                ),
            )
            self._audit(db, user["id"], "add", "weight", cursor.lastrowid, {"weight_kg": weight})
            return dict(db.execute("SELECT * FROM weight_logs WHERE id=?", (cursor.lastrowid,)).fetchone())

    def cancel_water(
        self, *, client_telegram_id: int, water_id: int, expected_version: int
    ) -> dict[str, Any]:
        return self._cancel_own_log(
            telegram_id=client_telegram_id, table="water_logs", entity="water",
            row_id=water_id, expected_version=expected_version,
        )

    def cancel_weight(
        self, *, client_telegram_id: int, weight_id: int, expected_version: int
    ) -> dict[str, Any]:
        return self._cancel_own_log(
            telegram_id=client_telegram_id, table="weight_logs", entity="weight",
            row_id=weight_id, expected_version=expected_version,
        )

    def _cancel_own_log(
        self,
        *,
        telegram_id: int,
        table: str,
        entity: str,
        row_id: int,
        expected_version: int,
    ) -> dict[str, Any]:
        if table not in {"water_logs", "weight_logs"}:
            raise ValueError("Неизвестный тип записи")
        user = self._require_user(telegram_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT * FROM {table} WHERE id=? AND client_user_id=?", (row_id, user["id"])
            ).fetchone()
            if row is None:
                raise PermissionError("Запись не найдена")
            if int(row["version"]) != int(expected_version):
                raise RuntimeError("stale_version")
            if row["status"] == "cancelled":
                return dict(row)
            db.execute(
                f"UPDATE {table} SET status='cancelled', cancelled_at=?, version=version+1 WHERE id=?",
                (_utc_now(), row_id),
            )
            self._audit(db, user["id"], "cancel", entity, row_id, {})
            return dict(db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())

    def update_water(
        self,
        *,
        client_telegram_id: int,
        water_id: int,
        amount_ml: Any,
        logged_at: str | datetime,
        expected_version: int,
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
        logged = _parse_datetime(logged_at)
        local_date = logged.astimezone(ZoneInfo(user["timezone"])).date().isoformat()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM water_logs WHERE id=? AND client_user_id=?",
                (water_id, user["id"]),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise PermissionError("Запись воды не найдена")
            if int(row["version"]) != int(expected_version):
                raise RuntimeError("stale_version")
            db.execute(
                """
                UPDATE water_logs SET amount_ml=?,logged_at=?,local_date=?,timezone=?,
                    version=version+1 WHERE id=?
                """,
                (
                    amount, logged.isoformat(timespec="seconds"), local_date,
                    user["timezone"], water_id,
                ),
            )
            self._audit(
                db, user["id"], "update", "water", water_id,
                {"before": {"amount_ml": row["amount_ml"], "logged_at": row["logged_at"]},
                 "after": {"amount_ml": amount, "logged_at": logged.isoformat(timespec="seconds")}},
            )
            return dict(db.execute("SELECT * FROM water_logs WHERE id=?", (water_id,)).fetchone())

    def update_weight(
        self,
        *,
        client_telegram_id: int,
        weight_id: int,
        weight_kg: Any,
        measured_at: str | datetime,
        expected_version: int,
        note: str = "",
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        weight = _number(weight_kg, "Вес", max_value=1000)
        if weight is None or weight <= 0:
            raise ValueError("Вес должен быть больше нуля")
        measured = _parse_datetime(measured_at)
        local_date = measured.astimezone(ZoneInfo(user["timezone"])).date().isoformat()
        clean_note = str(note or "").strip()
        if len(clean_note) > 500:
            raise ValueError("Заметка о весе слишком длинная")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM weight_logs WHERE id=? AND client_user_id=?",
                (weight_id, user["id"]),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise PermissionError("Запись веса не найдена")
            if int(row["version"]) != int(expected_version):
                raise RuntimeError("stale_version")
            db.execute(
                """
                UPDATE weight_logs SET weight_kg=?,measured_at=?,local_date=?,timezone=?,note=?,
                    version=version+1 WHERE id=?
                """,
                (
                    weight, measured.isoformat(timespec="seconds"), local_date,
                    user["timezone"], clean_note, weight_id,
                ),
            )
            self._audit(
                db, user["id"], "update", "weight", weight_id,
                {"before": {"weight_kg": row["weight_kg"], "measured_at": row["measured_at"]},
                 "after": {"weight_kg": weight, "measured_at": measured.isoformat(timespec="seconds")}},
            )
            return dict(db.execute("SELECT * FROM weight_logs WHERE id=?", (weight_id,)).fetchone())

    def get_own_meal(self, *, client_telegram_id: int, meal_id: int) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            self._require_owned_meal(db, user["id"], meal_id)
            return self._meal_by_id(db, meal_id)

    def get_own_water(self, *, client_telegram_id: int, water_id: int) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM water_logs WHERE id=? AND client_user_id=?",
                (water_id, user["id"]),
            ).fetchone()
            if row is None:
                raise PermissionError("Запись воды не найдена")
            return dict(row)

    def get_own_weight(self, *, client_telegram_id: int, weight_id: int) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM weight_logs WHERE id=? AND client_user_id=?",
                (weight_id, user["id"]),
            ).fetchone()
            if row is None:
                raise PermissionError("Запись веса не найдена")
            return dict(row)

    def update_meal_as_client(
        self,
        *,
        client_telegram_id: int,
        meal_id: int,
        updates: dict[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        allowed = {"eaten_at", "meal_type", "note", "hunger_level", "mood", "items"}
        if not updates or set(updates) - allowed:
            raise ValueError("Прием пищи не содержит поддерживаемых изменений")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            meal = self._require_owned_meal(db, user["id"], meal_id)
            if meal["status"] == "cancelled":
                raise ValueError("Отмененную запись нельзя исправить")
            if int(meal["version"]) != int(expected_version):
                raise RuntimeError("stale_version")
            before = self._meal_by_id(db, meal_id)
            sql_updates: list[str] = []
            values: list[Any] = []
            if "eaten_at" in updates:
                eaten = _parse_datetime(updates["eaten_at"])
                sql_updates.extend(["eaten_at=?", "local_date=?", "timezone=?"])
                values.extend([
                    eaten.isoformat(timespec="seconds"),
                    eaten.astimezone(ZoneInfo(user["timezone"])).date().isoformat(),
                    user["timezone"],
                ])
            if "meal_type" in updates:
                meal_type = str(updates["meal_type"] or "").strip()
                if not meal_type:
                    raise ValueError("Укажите тип приема пищи")
                sql_updates.append("meal_type=?")
                values.append(meal_type[:100])
            if "note" in updates:
                sql_updates.append("note=?")
                values.append(str(updates["note"] or "").strip()[:1000])
            hunger, mood = self._normalize_meal_context(
                updates.get("hunger_level", meal["hunger_level"]),
                updates.get("mood", meal["mood"]),
            )
            if "hunger_level" in updates:
                sql_updates.append("hunger_level=?")
                values.append(hunger)
            if "mood" in updates:
                sql_updates.append("mood=?")
                values.append(mood)
            if "items" in updates:
                items = self._validate_items(updates["items"], require_portion=True)
                if not items:
                    raise ValueError("Нужен хотя бы один продукт или блюдо")
                db.execute("DELETE FROM meal_items WHERE meal_id=?", (meal_id,))
                self._insert_items(db, meal_id, items, manually_edited=True)
            sql_updates.extend(["updated_at=?", "version=version+1"])
            values.extend([_utc_now(), meal_id])
            db.execute(f"UPDATE meals SET {', '.join(sql_updates)} WHERE id=?", values)
            after = self._meal_by_id(db, meal_id)
            self._audit(
                db, user["id"], "client_update", "meal", meal_id,
                {"before": before, "after": after},
            )
            return after

    def list_own_meals(
        self,
        *,
        client_telegram_id: int,
        date_from: str | date,
        date_to: str | date,
        cursor: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            return self._list_meals_for_user(
                db, user["id"], date_from, date_to, cursor=cursor, limit=limit
            )

    def list_client_meals(
        self,
        trainer_telegram_id: int,
        client_id: int,
        *,
        date_from: str | date,
        date_to: str | date,
        cursor: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            self._require_trainer_access(db, trainer["id"], client_id)
            return self._list_meals_for_user(
                db, client_id, date_from, date_to, cursor=cursor, limit=limit
            )

    def _list_meals_for_user(
        self,
        db: sqlite3.Connection,
        user_id: int,
        date_from: str | date,
        date_to: str | date,
        *,
        cursor: int | None,
        limit: int,
    ) -> dict[str, Any]:
        start, end = _parse_date(date_from), _parse_date(date_to)
        if end < start or (end - start).days > 365:
            raise ValueError("Период должен быть от 1 до 366 дней")
        bounded = max(1, min(int(limit), 100))
        cursor_id = int(cursor) if cursor is not None else 9_223_372_036_854_775_807
        rows = db.execute(
            """
            SELECT id FROM meals WHERE client_user_id=? AND local_date BETWEEN ? AND ?
              AND status!='cancelled' AND id<? ORDER BY id DESC LIMIT ?
            """,
            (user_id, start.isoformat(), end.isoformat(), cursor_id, bounded + 1),
        ).fetchall()
        has_more = len(rows) > bounded
        page = rows[:bounded]
        meals = [self._meal_by_id(db, row["id"]) for row in page]
        return {"items": meals, "next_cursor": page[-1]["id"] if has_more else None}

    def get_own_summary(
        self, *, client_telegram_id: int, date_from: str | date, date_to: str | date
    ) -> dict[str, Any]:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            return self._period_summary(db, user, date_from, date_to)

    def get_client_summary(
        self,
        trainer_telegram_id: int,
        client_id: int,
        date_from: str | date,
        date_to: str | date,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            client = self._require_trainer_access(db, trainer["id"], client_id)
            return self._period_summary(db, client, date_from, date_to)

    def get_own_norms(self, *, client_telegram_id: int, on_date: str | date) -> dict[str, Any] | None:
        user = self._require_user(client_telegram_id)
        with self._connection() as db:
            return self._effective_norms(db, user["id"], _parse_date(on_date))

    def get_norms_plan(
        self,
        *,
        trainer_telegram_id: int,
        client_id: int,
    ) -> list[dict[str, Any]]:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            self._require_trainer_access(db, trainer["id"], client_id)
            return [
                dict(row) for row in db.execute(
                    "SELECT * FROM nutrition_norms WHERE client_user_id=? ORDER BY effective_from,id",
                    (client_id,),
                ).fetchall()
            ]

    def get_meal_photo_file_id(
        self,
        *,
        actor_telegram_id: int,
        meal_id: int,
        trainer: bool,
    ) -> str | None:
        actor = self._require_user(actor_telegram_id)
        with self._connection() as db:
            meal = db.execute("SELECT * FROM meals WHERE id=?", (meal_id,)).fetchone()
            if meal is None:
                raise PermissionError("Прием пищи не найден")
            if trainer:
                self._require_trainer_access(db, actor["id"], meal["client_user_id"])
            elif meal["client_user_id"] != actor["id"]:
                raise PermissionError("Прием пищи не найден")
            return meal["photo_file_id"]

    def create_plan_preview(
        self,
        *,
        trainer_telegram_id: int,
        client_id: int,
        rows: Iterable[dict[str, Any]],
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        trainer = self._require_user(trainer_telegram_id)
        if ttl_seconds < 60 or ttl_seconds > 3600:
            raise ValueError("Недопустимый срок предпросмотра")
        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows:
            effective = _parse_date(raw.get("effective_from", "")).isoformat()
            if effective in seen:
                raise ValueError(f"Дата {effective} указана несколько раз")
            seen.add(effective)
            norms = self._normalize_norms(
                {key: value for key, value in raw.items() if key not in {"effective_from", "source_row"}}
            )
            prepared.append({"effective_from": effective, **norms})
        if not prepared or len(prepared) > 366:
            raise ValueError("План должен содержать от 1 до 366 строк")
        prepared.sort(key=lambda row: row["effective_from"])
        raw_token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_trainer_access(db, trainer["id"], client_id)
            dates = [row["effective_from"] for row in prepared]
            placeholders = ",".join("?" for _ in dates)
            conflicts = [
                row["effective_from"] for row in db.execute(
                    f"SELECT effective_from FROM nutrition_norms WHERE client_user_id=? "
                    f"AND effective_from IN ({placeholders}) ORDER BY effective_from",
                    (client_id, *dates),
                ).fetchall()
            ]
            db.execute(
                """
                UPDATE nutrition_plan_previews SET consumed_at=?
                WHERE trainer_user_id=? AND consumed_at IS NULL
                """,
                (now.isoformat(timespec="seconds"), trainer["id"]),
            )
            db.execute(
                """
                INSERT INTO nutrition_plan_previews(
                    token_hash,trainer_user_id,client_user_id,rows_json,conflicts_json,
                    expires_at,created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    self._token_hash(raw_token), trainer["id"], client_id,
                    json.dumps(prepared, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(conflicts, ensure_ascii=False, separators=(",", ":")),
                    expires.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
                ),
            )
        return {
            "upload_token": raw_token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "rows": prepared,
            "conflicts": conflicts,
        }

    def commit_plan_preview(
        self,
        *,
        trainer_telegram_id: int,
        client_id: int,
        upload_token: str,
    ) -> list[dict[str, Any]]:
        trainer = self._require_user(trainer_telegram_id)
        now = datetime.now(timezone.utc)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_trainer_access(db, trainer["id"], client_id)
            preview = db.execute(
                "SELECT * FROM nutrition_plan_previews WHERE token_hash=?",
                (self._token_hash(upload_token),),
            ).fetchone()
            if (
                preview is None
                or preview["trainer_user_id"] != trainer["id"]
                or preview["client_user_id"] != client_id
                or preview["consumed_at"] is not None
                or _parse_datetime(preview["expires_at"]) <= now
            ):
                raise PermissionError("Предпросмотр недействителен")
            rows = json.loads(preview["rows_json"])
            result = self._apply_norm_rows(
                db, actor_user_id=trainer["id"], client_id=client_id, rows=rows
            )
            db.execute(
                "UPDATE nutrition_plan_previews SET consumed_at=? WHERE token_hash=?",
                (now.isoformat(timespec="seconds"), preview["token_hash"]),
            )
            return result

    @staticmethod
    def _apply_norm_rows(
        db: sqlite3.Connection,
        *,
        actor_user_id: int,
        client_id: int,
        rows: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            effective = row["effective_from"]
            before = db.execute(
                "SELECT * FROM nutrition_norms WHERE client_user_id=? AND effective_from=?",
                (client_id, effective),
            ).fetchone()
            db.execute(
                """
                INSERT INTO nutrition_norms(
                    client_user_id,effective_from,calories,protein_g,fat_g,carbs_g,
                    water_ml,set_by_user_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(client_user_id,effective_from) DO UPDATE SET
                    calories=excluded.calories,protein_g=excluded.protein_g,
                    fat_g=excluded.fat_g,carbs_g=excluded.carbs_g,
                    water_ml=excluded.water_ml,set_by_user_id=excluded.set_by_user_id,
                    created_at=excluded.created_at
                """,
                (
                    client_id, effective, row.get("calories"), row.get("protein_g"),
                    row.get("fat_g"), row.get("carbs_g"), row.get("water_ml"),
                    actor_user_id, _utc_now(),
                ),
            )
            saved = db.execute(
                "SELECT * FROM nutrition_norms WHERE client_user_id=? AND effective_from=?",
                (client_id, effective),
            ).fetchone()
            NutritionStore._audit(
                db, actor_user_id, "set_norms", "user", client_id,
                {"before": dict(before) if before else None, "after": dict(saved)},
            )
            result.append(dict(saved))
        return result

    def get_trainer_for_client(self, *, client_telegram_id: int) -> dict[str, Any] | None:
        client = self._require_user(client_telegram_id)
        with self._connection() as db:
            row = db.execute(
                """
                SELECT u.id,u.display_name,u.timezone,tc.linked_at
                FROM trainer_clients tc JOIN users u ON u.id=tc.trainer_user_id
                WHERE tc.client_user_id=? AND tc.active=1
                ORDER BY tc.linked_at DESC LIMIT 1
                """,
                (client["id"],),
            ).fetchone()
            return dict(row) if row else None

    def get_client_chat_username(
        self, *, trainer_telegram_id: int, client_id: int
    ) -> str:
        trainer = self._require_user(trainer_telegram_id)
        with self._connection() as db:
            client = self._require_trainer_access(db, trainer["id"], client_id)
            username = str(client.get("telegram_username") or "")
            if not username:
                raise ValueError("У клиента не указан Telegram username")
            return username

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

    def list_client_reminder_sources(
        self, *, after_user_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT p.*,u.telegram_id,u.timezone
                FROM nutrition_client_reminder_preferences p
                JOIN users u ON u.id=p.user_id
                WHERE p.enabled=1 AND (p.meal_enabled=1 OR p.water_enabled=1)
                  AND p.user_id>?
                ORDER BY p.user_id LIMIT ?
                """,
                (max(0, int(after_user_id)), bounded),
            ).fetchall()
            return [self._client_reminder_source(row) for row in rows]

    def list_trainer_reminder_sources(
        self, *, after_user_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with self._connection() as db:
            rows = db.execute(
                """
                SELECT p.*,u.telegram_id,u.timezone
                FROM nutrition_trainer_reminder_preferences p
                JOIN users u ON u.id=p.user_id
                WHERE p.enabled=1 AND p.digest_enabled=1 AND p.user_id>?
                ORDER BY p.user_id LIMIT ?
                """,
                (max(0, int(after_user_id)), bounded),
            ).fetchall()
            return [self._trainer_reminder_source(row) for row in rows]

    def enqueue_notification(
        self,
        *,
        recipient_user_id: int,
        subject_user_id: int,
        kind: str,
        local_date: str | date,
        slot_key: str,
        preferences_scope: str,
        preferences_version: int,
        scheduled_for: str | datetime,
    ) -> bool:
        if kind not in {"meal", "water", "trainer_digest"}:
            raise ValueError("Неизвестный тип напоминания")
        if preferences_scope not in {"client", "trainer"}:
            raise ValueError("Неизвестная область настроек")
        scheduled = _parse_datetime(scheduled_for).isoformat(timespec="seconds")
        day = _parse_date(local_date).isoformat()
        slot = str(slot_key).strip()
        if not slot or len(slot) > 100:
            raise ValueError("Некорректный слот напоминания")
        with self._connection() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO nutrition_notification_outbox(
                    recipient_user_id,subject_user_id,kind,local_date,slot_key,
                    preferences_scope,preferences_version,scheduled_for,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(recipient_user_id), int(subject_user_id), kind, day, slot,
                    preferences_scope, int(preferences_version), scheduled, _utc_now(),
                ),
            )
            return cursor.rowcount == 1

    def claim_due_notifications(
        self,
        *,
        now: str | datetime,
        grace_seconds: int = 600,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        instant = _parse_datetime(now)
        if grace_seconds < 60 or grace_seconds > 3600:
            raise ValueError("Недопустимое окно доставки")
        bounded = max(1, min(int(limit), 100))
        oldest = instant - timedelta(seconds=grace_seconds)
        now_text = instant.isoformat(timespec="seconds")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE nutrition_notification_outbox
                SET status='cancelled',cancelled_at=?
                WHERE status IN ('pending','sending') AND scheduled_for < ?
                """,
                (now_text, oldest.isoformat(timespec="seconds")),
            )
            rows = db.execute(
                """
                SELECT * FROM nutrition_notification_outbox
                WHERE status='pending' AND scheduled_for<=?
                ORDER BY scheduled_for,id LIMIT ?
                """,
                (now_text, bounded),
            ).fetchall()
            result = []
            for row in rows:
                changed = db.execute(
                    """
                    UPDATE nutrition_notification_outbox
                    SET status='sending',claimed_at=?,attempts=attempts+1
                    WHERE id=? AND status='pending'
                    """,
                    (now_text, row["id"]),
                )
                if changed.rowcount == 1:
                    claimed = db.execute(
                        "SELECT * FROM nutrition_notification_outbox WHERE id=?", (row["id"],)
                    ).fetchone()
                    result.append(dict(claimed))
            return result

    def validate_notification_for_delivery(
        self,
        *,
        notification_id: int,
        allowed_trainer_ids: set[int] | None = None,
    ) -> dict[str, Any] | None:
        with self._connection() as db:
            job = db.execute(
                """
                SELECT o.*,u.telegram_id,u.timezone
                FROM nutrition_notification_outbox o
                JOIN users u ON u.id=o.recipient_user_id
                WHERE o.id=? AND o.status='sending'
                """,
                (int(notification_id),),
            ).fetchone()
            if job is None:
                return None
            if job["preferences_scope"] == "client":
                prefs = db.execute(
                    "SELECT * FROM nutrition_client_reminder_preferences WHERE user_id=?",
                    (job["recipient_user_id"],),
                ).fetchone()
                active = bool(
                    prefs and prefs["enabled"] and int(prefs["version"]) == job["preferences_version"]
                    and ((job["kind"] == "meal" and prefs["meal_enabled"])
                         or (job["kind"] == "water" and prefs["water_enabled"]))
                )
            else:
                prefs = db.execute(
                    "SELECT * FROM nutrition_trainer_reminder_preferences WHERE user_id=?",
                    (job["recipient_user_id"],),
                ).fetchone()
                active = bool(
                    prefs and prefs["enabled"] and prefs["digest_enabled"]
                    and int(prefs["version"]) == job["preferences_version"]
                    and allowed_trainer_ids is not None
                    and int(job["telegram_id"]) in allowed_trainer_ids
                )
            if not active:
                db.execute(
                    "UPDATE nutrition_notification_outbox SET status='cancelled',cancelled_at=? WHERE id=?",
                    (_utc_now(), job["id"]),
                )
                return None
            return dict(job)

    def complete_notification(
        self,
        *,
        notification_id: int,
        sent: bool,
        error_code: str | None = None,
    ) -> None:
        safe_error = None
        if error_code:
            safe_error = re.sub(r"[^a-zA-Z0-9_.:-]", "_", str(error_code))[:120]
        with self._connection() as db:
            if sent:
                db.execute(
                    """
                    UPDATE nutrition_notification_outbox
                    SET status='sent',sent_at=?,last_error=NULL WHERE id=? AND status='sending'
                    """,
                    (_utc_now(), int(notification_id)),
                )
            else:
                db.execute(
                    """
                    UPDATE nutrition_notification_outbox
                    SET status='failed',last_error=? WHERE id=? AND status='sending'
                    """,
                    (safe_error or "delivery_failed", int(notification_id)),
                )

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        key = str(value or "").strip()
        if not key or len(key) > 200:
            raise ValueError("Некорректный ключ запроса")
        return key

    @staticmethod
    def _client_reminder_payload(user: Any, row: sqlite3.Row | None) -> dict[str, Any]:
        return {
            "version": int(row["version"]) if row else 0,
            "enabled": bool(row["enabled"]) if row else False,
            "meal": {
                "enabled": bool(row["meal_enabled"]) if row else False,
                "times": json.loads(row["meal_times_json"]) if row else [],
                "max_times": 3,
            },
            "water": {
                "enabled": bool(row["water_enabled"]) if row else False,
                "mode": row["water_mode"] if row else "times",
                "times": json.loads(row["water_times_json"]) if row else [],
                "interval_minutes": row["water_interval_minutes"] if row else None,
                "start_local": row["water_start_local"] if row else None,
                "end_local": row["water_end_local"] if row else None,
            },
            "quiet_hours": {
                "start": row["quiet_start"] if row else None,
                "end": row["quiet_end"] if row else None,
            },
            "timezone": user["timezone"],
            "updated_at": row["updated_at"] if row else None,
        }

    @staticmethod
    def _trainer_reminder_payload(user: Any, row: sqlite3.Row | None) -> dict[str, Any]:
        return {
            "version": int(row["version"]) if row else 0,
            "enabled": bool(row["enabled"]) if row else False,
            "daily_digest": {
                "enabled": bool(row["digest_enabled"]) if row else False,
                "time": row["digest_time"] if row else "20:00",
                "inactivity_days": int(row["inactivity_days"]) if row else 1,
                "calorie_comparison_enabled": bool(row["calorie_comparison_enabled"]) if row else False,
                "over_plan_percent": int(row["over_plan_percent"]) if row else 10,
            },
            "timezone": user["timezone"],
            "updated_at": row["updated_at"] if row else None,
        }

    @staticmethod
    def _client_reminder_source(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "user_id": row["user_id"], "telegram_id": row["telegram_id"],
            "timezone": row["timezone"], "version": int(row["version"]),
            "meal_enabled": bool(row["meal_enabled"]),
            "meal_times": json.loads(row["meal_times_json"]),
            "water_enabled": bool(row["water_enabled"]), "water_mode": row["water_mode"],
            "water_times": json.loads(row["water_times_json"]),
            "water_interval_minutes": row["water_interval_minutes"],
            "water_start_local": row["water_start_local"], "water_end_local": row["water_end_local"],
            "quiet_start": row["quiet_start"], "quiet_end": row["quiet_end"],
        }

    @staticmethod
    def _trainer_reminder_source(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "user_id": row["user_id"], "telegram_id": row["telegram_id"],
            "timezone": row["timezone"], "version": int(row["version"]),
            "digest_time": row["digest_time"], "inactivity_days": int(row["inactivity_days"]),
            "calorie_comparison_enabled": bool(row["calorie_comparison_enabled"]),
            "over_plan_percent": int(row["over_plan_percent"]),
        }

    @staticmethod
    def _normalize_client_reminders(
        current: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(updates, dict) or set(updates) - {"enabled", "meal", "water", "quiet_hours"}:
            raise ValueError("Настройки напоминаний содержат неизвестные поля")
        result = {
            "enabled": current["enabled"],
            "meal": {"enabled": current["meal"]["enabled"], "times": list(current["meal"]["times"])},
            "water": {
                key: current["water"][key]
                for key in ("enabled", "mode", "times", "interval_minutes", "start_local", "end_local")
            },
            "quiet_hours": dict(current["quiet_hours"]),
        }
        if "enabled" in updates:
            if not isinstance(updates["enabled"], bool):
                raise ValueError("Включение напоминаний должно быть true или false")
            result["enabled"] = updates["enabled"]
        if "meal" in updates:
            meal = updates["meal"]
            if not isinstance(meal, dict) or set(meal) - {"enabled", "times"}:
                raise ValueError("Некорректные настройки напоминаний о еде")
            if "enabled" in meal:
                if not isinstance(meal["enabled"], bool):
                    raise ValueError("Включение напоминаний о еде должно быть true или false")
                result["meal"]["enabled"] = meal["enabled"]
            if "times" in meal:
                result["meal"]["times"] = _local_times(meal["times"], "Время еды", limit=3)
        if "water" in updates:
            water = updates["water"]
            allowed = {"enabled", "mode", "times", "interval_minutes", "start_local", "end_local"}
            if not isinstance(water, dict) or set(water) - allowed:
                raise ValueError("Некорректные настройки напоминаний о воде")
            if "enabled" in water:
                if not isinstance(water["enabled"], bool):
                    raise ValueError("Включение напоминаний о воде должно быть true или false")
                result["water"]["enabled"] = water["enabled"]
            if "mode" in water:
                if water["mode"] not in {"times", "interval"}:
                    raise ValueError("Режим воды должен быть times или interval")
                result["water"]["mode"] = water["mode"]
            if "times" in water:
                result["water"]["times"] = _local_times(water["times"], "Время воды", limit=12)
            if "interval_minutes" in water:
                interval = water["interval_minutes"]
                if interval is None:
                    result["water"]["interval_minutes"] = None
                else:
                    result["water"]["interval_minutes"] = _bounded_integer(
                        interval, "Интервал воды", 60, 720
                    )
            for source, target in (("start_local", "start_local"), ("end_local", "end_local")):
                if source in water:
                    result["water"][target] = _local_time(water[source], "Граница интервала", allow_none=True)
        if "quiet_hours" in updates:
            quiet = updates["quiet_hours"]
            if not isinstance(quiet, dict) or set(quiet) - {"start", "end"}:
                raise ValueError("Некорректные тихие часы")
            for key in ("start", "end"):
                if key in quiet:
                    result["quiet_hours"][key] = _local_time(
                        quiet[key], "Тихие часы", allow_none=True
                    )
        quiet = result["quiet_hours"]
        if (quiet["start"] is None) != (quiet["end"] is None) or (
            quiet["start"] is not None and quiet["start"] == quiet["end"]
        ):
            raise ValueError("Укажите обе разные границы тихих часов или очистите обе")
        if result["enabled"] and result["meal"]["enabled"] and not result["meal"]["times"]:
            raise ValueError("Укажите хотя бы одно время напоминания о еде")
        water = result["water"]
        if result["enabled"] and water["enabled"]:
            if water["mode"] == "times" and not water["times"]:
                raise ValueError("Укажите хотя бы одно время напоминания о воде")
            if water["mode"] == "interval" and (
                water["interval_minutes"] is None
                or water["start_local"] is None
                or water["end_local"] is None
                or water["start_local"] >= water["end_local"]
            ):
                raise ValueError("Для интервала воды укажите интервал и границы дня по возрастанию")
        return result

    @staticmethod
    def _normalize_trainer_reminders(
        current: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(updates, dict) or set(updates) - {"enabled", "daily_digest"}:
            raise ValueError("Настройки сводки содержат неизвестные поля")
        result = {
            "enabled": current["enabled"],
            "daily_digest": dict(current["daily_digest"]),
        }
        if "enabled" in updates:
            if not isinstance(updates["enabled"], bool):
                raise ValueError("Включение сводки должно быть true или false")
            result["enabled"] = updates["enabled"]
        if "daily_digest" in updates:
            digest = updates["daily_digest"]
            allowed = {
                "enabled", "time", "inactivity_days", "calorie_comparison_enabled",
                "over_plan_percent",
            }
            if not isinstance(digest, dict) or set(digest) - allowed:
                raise ValueError("Некорректные настройки ежедневной сводки")
            for key in ("enabled", "calorie_comparison_enabled"):
                if key in digest:
                    if not isinstance(digest[key], bool):
                        raise ValueError("Флаги сводки должны быть true или false")
                    result["daily_digest"][key] = digest[key]
            if "time" in digest:
                result["daily_digest"]["time"] = _local_time(digest["time"], "Время сводки")
            if "inactivity_days" in digest:
                result["daily_digest"]["inactivity_days"] = _bounded_integer(
                    digest["inactivity_days"], "Порог отсутствия записей", 1, 7
                )
            if "over_plan_percent" in digest:
                result["daily_digest"]["over_plan_percent"] = _bounded_integer(
                    digest["over_plan_percent"], "Порог сравнения с планом", 0, 500
                )
        return result

    @staticmethod
    def _cancel_pending_notifications(
        db: sqlite3.Connection,
        *,
        recipient_user_id: int | None = None,
        subject_user_id: int | None = None,
    ) -> None:
        clauses = ["status IN ('pending','sending')"]
        values: list[Any] = []
        if recipient_user_id is not None:
            clauses.append("recipient_user_id=?")
            values.append(int(recipient_user_id))
        if subject_user_id is not None:
            clauses.append("subject_user_id=?")
            values.append(int(subject_user_id))
        if len(clauses) == 1:
            raise ValueError("Для отмены очереди нужен пользователь")
        values.extend([_utc_now()])
        db.execute(
            f"UPDATE nutrition_notification_outbox SET status='cancelled',cancelled_at=? "
            f"WHERE {' AND '.join(clauses)}",
            (values[-1], *values[:-1]),
        )

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
            method = str(raw.get("calculation_method", "manual")).strip().lower()
            if method not in {"legacy", "manual", "ai", "reference"}:
                raise ValueError("Неизвестный метод расчета КБЖУ")
            approximate = bool(raw.get("approximate", method == "ai" or weight is None))
            if require_portion and not ((weight is not None and weight > 0) or portion_text):
                raise ValueError(f"Укажите массу или описание порции для блюда: {name}")
            reference = {
                "reference_fdc_id": None,
                "reference_source": None,
                "reference_version": None,
                "reference_url": None,
                "reference_description": None,
                "reference_preparation": None,
                "reference_kcal_per_100g": None,
                "reference_protein_per_100g": None,
                "reference_fat_per_100g": None,
                "reference_carbs_per_100g": None,
            }
            if method == "reference":
                fdc_id = str(raw.get("reference_fdc_id", "")).strip()
                if not fdc_id.isdigit():
                    raise ValueError("У записи справочника отсутствует корректный FDC ID")
                for key in ("reference_source", "reference_version", "reference_url"):
                    value = str(raw.get(key, "")).strip()
                    if not value:
                        raise ValueError("У записи справочника неполные сведения об источнике")
                    reference[key] = value[:1000]
                reference["reference_fdc_id"] = fdc_id
                reference["reference_description"] = str(
                    raw.get("reference_description", "")
                ).strip()[:1000]
                reference["reference_preparation"] = str(
                    raw.get("reference_preparation", "") or ""
                ).strip()[:200] or None
                if weight is None or weight <= 0:
                    raise ValueError("Для расчета по справочнику укажите массу больше нуля")
                per_keys = {
                    "calories": "reference_kcal_per_100g",
                    "protein_g": "reference_protein_per_100g",
                    "fat_g": "reference_fat_per_100g",
                    "carbs_g": "reference_carbs_per_100g",
                }
                totals = {}
                grams_decimal = Decimal(str(weight))
                for total_key, per_key in per_keys.items():
                    per_value = _number(raw.get(per_key), per_key, max_value=100000)
                    reference[per_key] = per_value
                    totals[total_key] = float(
                        (Decimal(str(per_value)) * grams_decimal / Decimal("100")).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                    )
                approximate = False
            else:
                totals = {
                    "calories": _number(raw.get("calories", 0), "Калории", max_value=100000),
                    "protein_g": _number(raw.get("protein_g", raw.get("protein", 0)), "Белки", max_value=10000),
                    "fat_g": _number(raw.get("fat_g", raw.get("fat", 0)), "Жиры", max_value=10000),
                    "carbs_g": _number(raw.get("carbs_g", raw.get("carbs", 0)), "Углеводы", max_value=10000),
                }
            prepared.append(
                {
                    "name": name,
                    "weight_g": weight,
                    "portion_text": portion_text,
                    **totals,
                    "approximate": approximate,
                    "calculation_method": method,
                    **reference,
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
                fat_g, carbs_g, approximate, manually_edited, calculation_method,
                reference_fdc_id, reference_source, reference_version, reference_url,
                reference_description, reference_preparation, reference_kcal_per_100g,
                reference_protein_per_100g, reference_fat_per_100g,
                reference_carbs_per_100g, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    meal_id, item["name"], item["weight_g"], item["portion_text"],
                    item["calories"], item["protein_g"], item["fat_g"], item["carbs_g"],
                    int(item["approximate"]), int(manually_edited), item["calculation_method"],
                    item["reference_fdc_id"], item["reference_source"], item["reference_version"],
                    item["reference_url"], item["reference_description"],
                    item["reference_preparation"], item["reference_kcal_per_100g"],
                    item["reference_protein_per_100g"], item["reference_fat_per_100g"],
                    item["reference_carbs_per_100g"], now,
                )
                for item in items
            ],
        )

    @staticmethod
    def _update_item(
        db: sqlite3.Connection,
        item_id: int,
        item: dict[str, Any],
        *,
        manually_edited: bool,
    ) -> None:
        db.execute(
            """
            UPDATE meal_items SET
                name = ?, weight_g = ?, portion_text = ?, calories = ?, protein_g = ?,
                fat_g = ?, carbs_g = ?, approximate = ?, manually_edited = ?,
                calculation_method = ?, reference_fdc_id = ?, reference_source = ?,
                reference_version = ?, reference_url = ?, reference_description = ?,
                reference_preparation = ?, reference_kcal_per_100g = ?,
                reference_protein_per_100g = ?, reference_fat_per_100g = ?,
                reference_carbs_per_100g = ?
            WHERE id = ?
            """,
            (
                item["name"], item["weight_g"], item["portion_text"], item["calories"],
                item["protein_g"], item["fat_g"], item["carbs_g"],
                int(item["approximate"]), int(manually_edited), item["calculation_method"],
                item["reference_fdc_id"], item["reference_source"], item["reference_version"],
                item["reference_url"], item["reference_description"],
                item["reference_preparation"], item["reference_kcal_per_100g"],
                item["reference_protein_per_100g"], item["reference_fat_per_100g"],
                item["reference_carbs_per_100g"], item_id,
            ),
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
    def _normalize_meal_context(
        hunger_level: Any,
        mood: Any,
    ) -> tuple[int | None, str | None]:
        hunger: int | None = None
        if hunger_level not in (None, ""):
            if isinstance(hunger_level, bool):
                raise ValueError("Уровень голода должен быть целым числом от 1 до 10")
            try:
                numeric_hunger = Decimal(str(hunger_level))
                if (
                    not numeric_hunger.is_finite()
                    or numeric_hunger != numeric_hunger.to_integral_value()
                ):
                    raise ValueError
                hunger = int(numeric_hunger)
            except (TypeError, ValueError, ArithmeticError) as exc:
                raise ValueError("Уровень голода должен быть целым числом от 1 до 10") from exc
            if hunger < 1 or hunger > 10:
                raise ValueError("Уровень голода должен быть от 1 до 10")
        prepared_mood = None if mood in (None, "") else str(mood).strip().lower()
        if prepared_mood not in {None, "great", "good", "neutral", "low", "stressed"}:
            raise ValueError("Неизвестное значение настроения")
        return hunger, prepared_mood

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

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
        water_entries = [
            dict(row) for row in db.execute(
                """
                SELECT * FROM water_logs WHERE client_user_id=? AND local_date=? AND status='active'
                ORDER BY logged_at,id
                """,
                (user["id"], local_date.isoformat()),
            ).fetchall()
        ]
        water = db.execute(
            """
            SELECT COALESCE(SUM(amount_ml), 0) AS total
            FROM water_logs WHERE client_user_id = ? AND local_date = ? AND status = 'active'
            """,
            (user["id"], local_date.isoformat()),
        ).fetchone()["total"]
        weight_entries = [
            dict(row) for row in db.execute(
                """
                SELECT * FROM weight_logs WHERE client_user_id=? AND local_date=? AND status='active'
                ORDER BY measured_at,id
                """,
                (user["id"], local_date.isoformat()),
            ).fetchall()
        ]
        weight = db.execute(
            """
            SELECT weight_kg FROM weight_logs
            WHERE client_user_id=? AND local_date=? AND status='active'
            ORDER BY measured_at DESC,id DESC LIMIT 1
            """,
            (user["id"], local_date.isoformat()),
        ).fetchone()
        client = {key: user[key] for key in ("id", "telegram_id", "display_name", "timezone")}
        return {
            "client": client,
            "date": local_date.isoformat(),
            "norms": self._effective_norms(db, user["id"], local_date),
            "meals": meals,
            "water_ml": int(water or 0),
            "water_entries": water_entries,
            "weight_kg": weight["weight_kg"] if weight else None,
            "weight_entries": weight_entries,
            "totals": {key: round(value, 2) for key, value in totals.items()},
        }

    def _week_summary(
        self,
        db: sqlite3.Connection,
        user: dict[str, Any],
        week_start: date,
    ) -> dict[str, Any]:
        days = [self._day_summary(db, user, week_start + timedelta(days=offset)) for offset in range(7)]
        meal_days = [day for day in days if day["meals"]]
        water_days = [day for day in days if day["water_entries"]]
        averages = {
            field: (
                round(sum(day["totals"][field] for day in meal_days) / len(meal_days), 2)
                if meal_days else None
            )
            for field in ("calories", "protein_g", "fat_g", "carbs_g")
        }
        averages["water_ml"] = (
            round(sum(day["water_ml"] for day in water_days) / len(water_days), 2)
            if water_days else None
        )
        return {
            "client": days[0]["client"],
            "week_start": week_start.isoformat(),
            "days": days,
            "averages": averages,
            "coverage": {
                "nutrition_logged_days": len(meal_days),
                "water_logged_days": len(water_days),
                "period_days": 7,
            },
        }

    def _period_summary(
        self,
        db: sqlite3.Connection,
        user: dict[str, Any],
        date_from: str | date,
        date_to: str | date,
    ) -> dict[str, Any]:
        start, end = _parse_date(date_from), _parse_date(date_to)
        if end < start or (end - start).days > 365:
            raise ValueError("Период должен быть от 1 до 366 дней")
        nutrition_rows = {
            row["local_date"]: dict(row)
            for row in db.execute(
                """
                SELECT m.local_date,COUNT(DISTINCT m.id) AS meal_count,
                       SUM(i.calories) AS calories,SUM(i.protein_g) AS protein_g,
                       SUM(i.fat_g) AS fat_g,SUM(i.carbs_g) AS carbs_g
                FROM meals m JOIN meal_items i ON i.meal_id=m.id
                WHERE m.client_user_id=? AND m.status='confirmed'
                  AND m.local_date BETWEEN ? AND ?
                GROUP BY m.local_date
                """,
                (user["id"], start.isoformat(), end.isoformat()),
            ).fetchall()
        }
        water_rows = {
            row["local_date"]: int(row["water_ml"])
            for row in db.execute(
                """
                SELECT local_date,SUM(amount_ml) AS water_ml FROM water_logs
                WHERE client_user_id=? AND status='active' AND local_date BETWEEN ? AND ?
                GROUP BY local_date
                """,
                (user["id"], start.isoformat(), end.isoformat()),
            ).fetchall()
        }
        weight_rows: dict[str, float] = {}
        for row in db.execute(
            """
            SELECT local_date,weight_kg FROM weight_logs w
            WHERE client_user_id=? AND status='active' AND local_date BETWEEN ? AND ?
              AND id=(SELECT w2.id FROM weight_logs w2
                      WHERE w2.client_user_id=w.client_user_id
                        AND w2.local_date=w.local_date AND w2.status='active'
                      ORDER BY w2.measured_at DESC,w2.id DESC LIMIT 1)
            """,
            (user["id"], start.isoformat(), end.isoformat()),
        ).fetchall():
            weight_rows[row["local_date"]] = float(row["weight_kg"])
        series: list[dict[str, Any]] = []
        for offset in range((end - start).days + 1):
            key = (start + timedelta(days=offset)).isoformat()
            nutrition = nutrition_rows.get(key)
            series.append(
                {
                    "date": key,
                    "calories": round(float(nutrition["calories"]), 2) if nutrition else None,
                    "protein_g": round(float(nutrition["protein_g"]), 2) if nutrition else None,
                    "fat_g": round(float(nutrition["fat_g"]), 2) if nutrition else None,
                    "carbs_g": round(float(nutrition["carbs_g"]), 2) if nutrition else None,
                    "water_ml": water_rows.get(key),
                    "weight_kg": weight_rows.get(key),
                    "meal_count": int(nutrition["meal_count"]) if nutrition else 0,
                    "norms": self._effective_norms(db, user["id"], date.fromisoformat(key)),
                }
            )
        metric_names = ("calories", "protein_g", "fat_g", "carbs_g", "water_ml", "weight_kg")
        totals: dict[str, float | int | None] = {}
        averages: dict[str, float | None] = {}
        coverage: dict[str, dict[str, int]] = {}
        for name in metric_names:
            values = [float(day[name]) for day in series if day[name] is not None]
            totals[name] = round(sum(values), 2) if values else None
            averages[name] = round(sum(values) / len(values), 2) if values else None
            coverage[name] = {"logged_days": len(values), "period_days": len(series)}
        top_foods = [
            dict(row) for row in db.execute(
                """
                SELECT COALESCE(i.reference_fdc_id,'') AS fdc_id,
                       i.name AS display_name,i.calculation_method,
                       COUNT(*) AS entries,SUM(i.weight_g) AS total_weight_g
                FROM meals m JOIN meal_items i ON i.meal_id=m.id
                WHERE m.client_user_id=? AND m.status='confirmed'
                  AND m.local_date BETWEEN ? AND ?
                GROUP BY COALESCE(i.reference_fdc_id,'name:' || lower(i.name)),
                         i.name,i.calculation_method
                ORDER BY entries DESC,display_name LIMIT 10
                """,
                (user["id"], start.isoformat(), end.isoformat()),
            ).fetchall()
        ]
        return {
            "client": {
                "id": user["id"], "display_name": user["display_name"],
                "timezone": user["timezone"],
            },
            "period": {"from": start.isoformat(), "to": end.isoformat(), "bucket": "day"},
            "totals": totals,
            "averages": averages,
            "coverage": coverage,
            "days_with_entries": sum(
                1 for day in series
                if day["meal_count"] or day["water_ml"] is not None or day["weight_kg"] is not None
            ),
            "series": series,
            "top_foods": top_foods,
        }
