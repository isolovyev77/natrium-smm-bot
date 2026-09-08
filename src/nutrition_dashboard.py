"""Lightweight trainer dashboard for the nutrition diary.

The HTTP listener is intentionally bound to loopback by default.  A production
Telegram Mini App needs a separately configured HTTPS origin/reverse proxy.
Every API request is authenticated with Telegram WebApp ``initData`` and every
object lookup is delegated to ``NutritionStore`` with the authenticated trainer
id, so the store remains the final authorization boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import io
import json
import math
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, parse_qsl, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from src.nutrition_plan import MAX_FILE_BYTES, PlanValidationError, build_template, parse_plan
from src.nutrition_reference import FoodReference, FoodReferenceError
from src.nutrition_export import build_csv, build_print_html, build_xlsx


INIT_DATA_HEADER = "X-Telegram-Init-Data"
DEFAULT_AUTH_TTL_SECONDS = 600
DEFAULT_FUTURE_SKEW_SECONDS = 30
MAX_INIT_DATA_BYTES = 16_384
MAX_JSON_BODY_BYTES = 65_536
MAX_TEXT_LENGTH = 2_000
MAX_NUMERIC_VALUE = 100_000.0  # Technical abuse bound, not a nutrition norm.
MAX_PHOTO_BYTES = 20 * 1024 * 1024


class InitDataError(ValueError):
    """Telegram initData is missing, malformed, invalid or too old."""


class TrainerNotAllowedError(PermissionError):
    """A valid Telegram user is not allowed to use the trainer dashboard."""


def _parse_positive_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for value in re.split(r"[,;\s]+", raw.strip()):
        if not value:
            continue
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError("Trainer allowlist contains a non-integer id") from exc
        if parsed <= 0:
            raise ValueError("Trainer allowlist ids must be positive")
        result.add(parsed)
    return result


def trainer_allowlist_from_env() -> set[int]:
    """Read trainer ids, with ADMIN_TELEGRAM_ID as a legacy fallback only."""

    configured = os.getenv("TRAINER_TELEGRAM_IDS", "").strip()
    if configured:
        return _parse_positive_ids(configured)
    fallback = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    return _parse_positive_ids(fallback) if fallback else set()


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    allowed_trainer_ids: Iterable[int] | None,
    *,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_AUTH_TTL_SECONDS,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
) -> dict[str, Any]:
    """Validate Telegram Mini App initData and return its user object.

    Implements Telegram's HMAC-SHA256 data-check-string algorithm.  The caller
    never supplies a trainer id separately: it is taken only from signed data.
    """

    if not isinstance(init_data, str) or not init_data:
        raise InitDataError("Telegram initData is required")
    if len(init_data.encode("utf-8")) > MAX_INIT_DATA_BYTES:
        raise InitDataError("Telegram initData is too large")
    if not bot_token:
        raise InitDataError("Bot token is not configured")
    if ttl_seconds <= 0 or future_skew_seconds < 0:
        raise ValueError("Invalid initData time bounds")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("Malformed Telegram initData") from exc
    if not pairs:
        raise InitDataError("Malformed Telegram initData")

    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise InitDataError("Duplicate initData field")
        values[key] = value

    received_hash = values.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", received_hash):
        raise InitDataError("Invalid initData signature")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(received_hash.lower(), expected_hash):
        raise InitDataError("Invalid initData signature")

    try:
        auth_date = int(values["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InitDataError("Invalid auth_date") from exc
    current_time = int(time.time()) if now is None else int(now)
    if auth_date > current_time + future_skew_seconds:
        raise InitDataError("initData auth_date is in the future")
    if auth_date < current_time - ttl_seconds:
        raise InitDataError("initData has expired")

    try:
        user = json.loads(values["user"])
        trainer_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InitDataError("Invalid Telegram user") from exc
    if not isinstance(user, dict) or trainer_id <= 0:
        raise InitDataError("Invalid Telegram user")

    if allowed_trainer_ids is not None:
        allowlist = {int(item) for item in allowed_trainer_ids}
        if trainer_id not in allowlist:
            raise TrainerNotAllowedError("Telegram user is not an allowed trainer")
    return user


def verify_telegram_user_init_data(
    init_data: str,
    bot_token: str,
    *,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_AUTH_TTL_SECONDS,
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
) -> dict[str, Any]:
    """Validate signed Telegram data for either a client or a trainer."""

    user = verify_telegram_init_data(
        init_data,
        bot_token,
        None,
        now=now,
        ttl_seconds=ttl_seconds,
        future_skew_seconds=future_skew_seconds,
    )
    return user


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _safe_json(value: Any) -> bytes:
    # Escaping HTML metacharacters adds defense in depth if a response is ever
    # embedded incorrectly.  The frontend also uses textContent exclusively.
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return payload.encode("utf-8")


def _parse_iso_date(raw: str, field_name: str) -> str:
    try:
        return date.fromisoformat(raw).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _positive_int(raw: str, field_name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def _finite_nonnegative(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0 or parsed > MAX_NUMERIC_VALUE:
        raise ValueError(f"{field_name} is outside the accepted range")
    return parsed


def _bounded_text(value: Any, field_name: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError(f"{field_name} is required")
    if len(cleaned) > MAX_TEXT_LENGTH:
        raise ValueError(f"{field_name} is too long")
    return cleaned


def _validate_norms(payload: Mapping[str, Any]) -> tuple[dict[str, float], str]:
    allowed = {"calories", "protein_g", "fat_g", "carbs_g", "water_ml", "effective_from"}
    if set(payload) - allowed:
        raise ValueError("Unexpected norms field")
    effective_from = _parse_iso_date(payload.get("effective_from", ""), "effective_from")
    nutrition_keys = allowed - {"effective_from"}
    if not any(key in payload for key in nutrition_keys):
        raise ValueError("At least one norm is required")
    norms = {
        key: _finite_nonnegative(payload[key], key)
        for key in nutrition_keys
        if key in payload
    }
    return norms, effective_from


def _validate_meal_updates(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"eaten_at", "meal_type", "note", "items"}
    if not payload or set(payload) - allowed:
        raise ValueError("Unexpected or empty meal update")
    result: dict[str, Any] = {}
    if "eaten_at" in payload:
        try:
            eaten_at = datetime.fromisoformat(str(payload["eaten_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("eaten_at must be an ISO datetime") from exc
        if eaten_at.tzinfo is None:
            raise ValueError("eaten_at must contain a timezone")
        result["eaten_at"] = eaten_at.isoformat()
    if "meal_type" in payload:
        result["meal_type"] = _bounded_text(payload["meal_type"], "meal_type", required=True)
    if "note" in payload:
        result["note"] = _bounded_text(payload["note"], "note")
    if "items" in payload:
        items = payload["items"]
        if not isinstance(items, list) or not items or len(items) > 100:
            raise ValueError("items must be a non-empty list")
        prepared = []
        item_fields = {
            "name", "weight_g", "portion_text", "calories", "protein_g",
            "fat_g", "carbs_g", "approximate",
        }
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) - item_fields:
                raise ValueError(f"Invalid item at position {index + 1}")
            name = _bounded_text(item.get("name"), "name", required=True)
            portion_text = _bounded_text(item.get("portion_text", ""), "portion_text")
            weight_g = None
            if item.get("weight_g") not in (None, ""):
                weight_g = _finite_nonnegative(item["weight_g"], "weight_g")
            if not ((weight_g is not None and weight_g > 0) or portion_text):
                raise ValueError(f"Portion is required for item {index + 1}")
            approximate = item.get("approximate", True)
            if not isinstance(approximate, bool):
                raise ValueError("approximate must be boolean")
            prepared.append(
                {
                    "name": name,
                    "weight_g": weight_g,
                    "portion_text": portion_text,
                    "calories": _finite_nonnegative(item.get("calories", 0), "calories"),
                    "protein_g": _finite_nonnegative(item.get("protein_g", 0), "protein_g"),
                    "fat_g": _finite_nonnegative(item.get("fat_g", 0), "fat_g"),
                    "carbs_g": _finite_nonnegative(item.get("carbs_g", 0), "carbs_g"),
                    "approximate": approximate,
                }
            )
        result["items"] = prepared
    return result


@dataclass(frozen=True)
class DashboardConfig:
    bot_token: str
    allowed_trainer_ids: frozenset[int]
    auth_ttl_seconds: int = DEFAULT_AUTH_TTL_SECONDS
    future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS
    allowed_origin: str | None = None
    html_path: Path = Path(__file__).with_name("nutrition_dashboard.html")
    now: Callable[[], int] = lambda: int(time.time())
    session_ttl_seconds: int = 43_200
    photo_loader: Callable[[str], tuple[str, bytes]] | None = None
    secure_cookies: bool = True


class _DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, max_workers: int = 16, read_timeout: float = 10.0, **kwargs: Any):
        self._slots = threading.BoundedSemaphore(max_workers)
        self._read_timeout = read_timeout
        super().__init__(*args, **kwargs)

    def get_request(self) -> tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(self._read_timeout)
        return request, address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _csrf_for_session(bot_token: str, session_token: str) -> str:
    return hmac.new(
        bot_token.encode("utf-8"),
        ("nutrition-csrf:" + session_token).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _public_payload(value: Any) -> Any:
    private = {
        "telegram_id", "telegram_username", "photo_file_id", "confirmation_key", "idempotency_key",
        "token_hash", "session_hash", "csrf_hash", "rows_json", "conflicts_json",
    }
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in private:
                if key == "photo_file_id":
                    result["has_photo"] = bool(item)
                continue
            result[key] = _public_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_payload(item) for item in value]
    return value


def _telegram_photo_loader(bot_token: str, file_id: str) -> tuple[str, bytes]:
    request = Request(
        f"https://api.telegram.org/bot{bot_token}/getFile",
        data=json.dumps({"file_id": file_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read(256 * 1024))
    path = str((payload.get("result") or {}).get("file_path") or "")
    if not payload.get("ok") or not path or ".." in path or path.startswith("/"):
        raise RuntimeError("photo_unavailable")
    with urlopen(f"https://api.telegram.org/file/bot{bot_token}/{path}", timeout=20) as response:
        content_type = response.headers.get_content_type()
        content = response.read(MAX_PHOTO_BYTES + 1)
    if len(content) > MAX_PHOTO_BYTES or not content_type.startswith("image/"):
        raise RuntimeError("photo_unavailable")
    return content_type, content


def _handler_factory(store: Any, config: DashboardConfig) -> type[BaseHTTPRequestHandler]:
    reference: FoodReference | None
    try:
        reference = FoodReference()
    except Exception:
        reference = None

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "NatriumNutritionDashboard"
        sys_version = ""

        def log_message(self, _format: str, *_args: Any) -> None:
            # Avoid accidental logging of paths, request bodies or initData.
            return

        def _security_headers(self, *, nonce: str | None = None) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if nonce:
                policy = (
                    "default-src 'self'; base-uri 'none'; object-src 'none'; "
                    f"script-src 'self' 'nonce-{nonce}' https://telegram.org; "
                    f"style-src 'nonce-{nonce}'; img-src 'self' data: blob:; "
                    "connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org"
                )
                self.send_header("Content-Security-Policy", policy)
            if config.allowed_origin:
                self.send_header("Access-Control-Allow-Origin", config.allowed_origin)
                self.send_header("Vary", "Origin")

        def _send_json(self, status: int, payload: Any) -> None:
            body = _safe_json(_public_payload(payload))
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            for key, value in getattr(self, "_extra_headers", []):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            filename: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self._security_headers()
            for key, value in getattr(self, "_extra_headers", []):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _error(
            self,
            status: int,
            message: str,
            *,
            code: str | None = None,
            fields: dict[str, str] | None = None,
        ) -> None:
            default_codes = {
                400: "invalid_request", 401: "authentication_required",
                403: "forbidden", 404: "not_found", 409: "stale_version",
                422: "validation_failed", 429: "rate_limited", 500: "internal_error",
            }
            self._send_json(status, {
                "error": {
                    "code": code or default_codes.get(status, "request_failed"),
                    "message": message,
                    "fields": fields or {},
                }
            })

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or (config.allowed_origin is not None and origin == config.allowed_origin)

        def _cookie_session_token(self) -> str:
            raw = self.headers.get("Cookie", "")
            if not raw:
                return ""
            try:
                cookie = SimpleCookie()
                cookie.load(raw)
                cookie_name = "__Host-natrium_session" if config.secure_cookies else "nutrition_preview_session"
                value = cookie.get(cookie_name)
                return value.value if value else ""
            except Exception:
                return ""

        def _authenticate(self, *, trainer: bool = False) -> tuple[int, str, str | None]:
            if not self._origin_allowed():
                raise TrainerNotAllowedError("Origin is not allowed")
            init_data = self.headers.get(INIT_DATA_HEADER, "")
            if init_data:
                user = verify_telegram_user_init_data(
                    init_data, config.bot_token, now=config.now(),
                    ttl_seconds=config.auth_ttl_seconds,
                    future_skew_seconds=config.future_skew_seconds,
                )
                telegram_id = int(user["id"])
                if trainer and telegram_id not in config.allowed_trainer_ids:
                    raise TrainerNotAllowedError("Telegram user is not an allowed trainer")
                return telegram_id, "telegram", None
            session_token = self._cookie_session_token()
            if not session_token:
                raise InitDataError("Authentication is required")
            try:
                session = store.authenticate_browser_session(session_token=session_token)
            except (PermissionError, ValueError) as exc:
                raise InitDataError("Browser session has expired") from exc
            telegram_id = int(session["telegram_id"])
            if trainer and telegram_id not in config.allowed_trainer_ids:
                raise TrainerNotAllowedError("Telegram user is not an allowed trainer")
            if self.command in {"POST", "PATCH", "DELETE"}:
                csrf = self.headers.get("X-CSRF-Token", "")
                if not csrf or not store.validate_browser_csrf(
                    session_token=session_token, csrf_token=csrf
                ):
                    raise TrainerNotAllowedError("CSRF token is invalid")
            return telegram_id, "cookie", session_token

        def _issue_session(self, telegram_id: int) -> tuple[str, str, str]:
            session_token = secrets.token_urlsafe(32)
            csrf = _csrf_for_session(config.bot_token, session_token)
            result = store.start_browser_session(
                telegram_id=telegram_id,
                session_token=session_token,
                csrf_token=csrf,
                ttl_seconds=config.session_ttl_seconds,
            )
            max_age = config.session_ttl_seconds
            secure = "; Secure" if config.secure_cookies else ""
            cookie_name = "__Host-natrium_session" if config.secure_cookies else "nutrition_preview_session"
            self._extra_headers = getattr(self, "_extra_headers", []) + [
                (
                    "Set-Cookie",
                    f"{cookie_name}={session_token}; Path=/; Max-Age={max_age}; "
                    f"HttpOnly; SameSite=Lax{secure}",
                )
            ]
            return session_token, csrf, result["expires_at"]

        def _read_json(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ValueError("Content-Length is required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length") from exc
            if length <= 0 or length > MAX_JSON_BODY_BYTES:
                raise ValueError("Invalid JSON body size")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Malformed JSON body") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _read_binary(self, expected_type: str, maximum: int) -> bytes:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != expected_type:
                raise ValueError("Неподдерживаемый тип файла")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise ValueError("Некорректный размер файла") from exc
            if length <= 0 or length > maximum:
                raise ValueError("Файл превышает допустимый размер")
            return self.rfile.read(length)

        @staticmethod
        def _expected_version(headers: Any) -> int:
            raw = str(headers.get("If-Match", "")).strip().strip('"')
            if not raw or not raw.isdigit():
                raise ValueError("Для изменения нужна актуальная версия")
            return int(raw)

        @staticmethod
        def _optional_expected_version(headers: Any) -> int | None:
            raw = str(headers.get("If-Match", "")).strip().strip('"')
            if not raw:
                return None
            if not raw.isdigit():
                raise ValueError("Некорректная версия записи")
            return int(raw)

        @staticmethod
        def _idempotency_key(headers: Any) -> str:
            value = str(headers.get("Idempotency-Key", "")).strip()
            if not value or len(value) > 200:
                raise ValueError("Для сохранения нужен ключ идемпотентности")
            return value

        @staticmethod
        def _date_range(query: dict[str, list[str]]) -> tuple[str, str]:
            start = _parse_iso_date(query.get("from", [""])[0], "from")
            end = _parse_iso_date(query.get("to", [""])[0], "to")
            if (date.fromisoformat(end) - date.fromisoformat(start)).days not in range(366):
                raise ValueError("Период должен быть от 1 до 366 дней")
            return start, end

        def _prepare_items(self, items: Any) -> list[dict[str, Any]]:
            if not isinstance(items, list) or not items or len(items) > 100:
                raise ValueError("Нужен список блюд")
            prepared = []
            for raw in items:
                if not isinstance(raw, dict):
                    raise ValueError("Некорректная запись блюда")
                if raw.get("fdc_id") not in (None, ""):
                    if reference is None:
                        raise RuntimeError("reference_unavailable")
                    calculated = reference.calculate(raw["fdc_id"], raw.get("grams"))
                    prepared.append(
                        {
                            "name": calculated.display_name,
                            "weight_g": float(calculated.grams),
                            "portion_text": "",
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
                    )
                    continue
                allowed = {
                    "name", "weight_g", "portion_text", "calories", "protein_g",
                    "fat_g", "carbs_g", "approximate",
                }
                if set(raw) - allowed:
                    raise ValueError("Ручная запись содержит лишние поля")
                item = {key: raw.get(key) for key in allowed}
                item["calculation_method"] = "manual"
                prepared.append(item)
            return prepared

        def _session_payload(self, telegram_id: int, auth_kind: str, session_token: str | None) -> dict[str, Any]:
            user = store.get_user_by_telegram_id(telegram_id)
            if user is None:
                raise TrainerNotAllowedError("User is not registered")
            if auth_kind == "telegram":
                session_token, csrf, expires_at = self._issue_session(telegram_id)
            else:
                csrf = _csrf_for_session(config.bot_token, session_token or "")
                session = store.authenticate_browser_session(session_token=session_token or "")
                expires_at = session["expires_at"]
            is_trainer = telegram_id in config.allowed_trainer_ids
            today = datetime.now(ZoneInfo(user["timezone"])).date().isoformat()
            return {
                "api_version": 3,
                "user": {
                    "id": user["id"], "display_name": user["display_name"],
                    "timezone": user["timezone"],
                },
                "roles": ["self"] + (["trainer"] if is_trainer else []),
                "today": today,
                "csrf_token": csrf,
                "session_expires_at": expires_at,
                "capabilities": {
                    "self_dashboard": True,
                    "trainer_dashboard": is_trainer,
                    "edit_own_diary": True,
                    "manage_clients": is_trainer,
                    "manage_norms": is_trainer,
                    "xlsx_plan": is_trainer,
                    "export_csv": True,
                    "export_xlsx": True,
                    "print_view": True,
                    "meal_photo": True,
                    "browser_login": True,
                    "open_client_chat": is_trainer,
                    "reminder_preferences": True,
                    "trainer_reminders": is_trainer,
                },
            }

        @staticmethod
        def _data(value: Any, **meta: Any) -> dict[str, Any]:
            return {"data": value, "meta": meta}

        def _dispatch(self, method: str) -> tuple[int, Any] | tuple[int, bytes, str, str | None]:
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query, keep_blank_values=True)

            if method == "POST" and path == "/api/auth/browser/exchange":
                if not self._origin_allowed():
                    raise TrainerNotAllowedError("Origin is not allowed")
                payload = self._read_json()
                if set(payload) != {"token"}:
                    raise ValueError("Некорректная ссылка")
                session_token = secrets.token_urlsafe(32)
                csrf = _csrf_for_session(config.bot_token, session_token)
                result = store.exchange_browser_login_token(
                    login_token=str(payload["token"]), session_token=session_token,
                    csrf_token=csrf, ttl_seconds=config.session_ttl_seconds,
                )
                self._extra_headers = [(
                    "Set-Cookie",
                    f"{'__Host-natrium_session' if config.secure_cookies else 'nutrition_preview_session'}="
                    f"{session_token}; Path=/; "
                    f"Max-Age={config.session_ttl_seconds}; HttpOnly; SameSite=Lax"
                    f"{'; Secure' if config.secure_cookies else ''}",
                )]
                return 200, self._data({"csrf_token": csrf, "session_expires_at": result["expires_at"]})

            if method == "GET" and path == "/api/session":
                actor, auth_kind, session_token = self._authenticate()
                return 200, self._data(self._session_payload(actor, auth_kind, session_token))

            if method == "POST" and path == "/api/auth/logout":
                actor, _, session_token = self._authenticate()
                if session_token:
                    store.revoke_browser_session(session_token=session_token)
                self._extra_headers = [(
                    "Set-Cookie",
                    f"{'__Host-natrium_session' if config.secure_cookies else 'nutrition_preview_session'}="
                    "; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                    + ("; Secure" if config.secure_cookies else ""),
                )]
                return 200, self._data({"ok": True, "user_id": actor})

            if method == "GET" and path == "/api/clients":
                trainer_id, _, _ = self._authenticate(trainer=True)
                clients = store.list_trainer_clients(trainer_id)
                return 200, {"clients": clients, **self._data({"items": clients})}

            match = re.fullmatch(r"/api/clients/(\d+)/day", path)
            if method == "GET" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                client_id = _positive_int(match.group(1), "client_id")
                local_date = _parse_iso_date(query.get("date", [""])[0], "date")
                result = store.get_client_day(trainer_id, client_id, local_date)
                return 200, {**result, **self._data(result)}

            match = re.fullmatch(r"/api/clients/(\d+)/week", path)
            if method == "GET" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                client_id = _positive_int(match.group(1), "client_id")
                week_start = _parse_iso_date(query.get("start", [""])[0], "start")
                result = store.get_client_week(trainer_id, client_id, week_start)
                return 200, {**result, **self._data(result)}

            match = re.fullmatch(r"/api/clients/(\d+)/summary", path)
            if method == "GET" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                start, end = self._date_range(query)
                return 200, self._data(store.get_client_summary(
                    trainer_id, _positive_int(match.group(1), "client_id"), start, end
                ))

            match = re.fullmatch(r"/api/clients/(\d+)/meals", path)
            if method == "GET" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                start, end = self._date_range(query)
                result = store.list_client_meals(
                    trainer_id, _positive_int(match.group(1), "client_id"),
                    date_from=start, date_to=end,
                    cursor=query.get("cursor", [None])[0],
                    limit=int(query.get("limit", [20])[0]),
                )
                return 200, self._data(result["items"], next_cursor=result["next_cursor"])

            match = re.fullmatch(r"/api/clients/(\d+)/norms", path)
            if method == "POST" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                client_id = _positive_int(match.group(1), "client_id")
                norms, effective_from = _validate_norms(self._read_json())
                result = store.set_norms(trainer_id, client_id, norms, effective_from)
                return 200, {"ok": True, "norms": result}

            match = re.fullmatch(r"/api/meals/(\d+)", path)
            if method == "GET" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                return 200, self._data(store.get_client_meal(
                    trainer_id, _positive_int(match.group(1), "meal_id")
                ))
            if method == "PATCH" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                meal_id = _positive_int(match.group(1), "meal_id")
                updates = _validate_meal_updates(self._read_json())
                result = store.update_meal_as_trainer(
                    trainer_id, meal_id, updates,
                    expected_version=self._expected_version(self.headers),
                )
                return 200, self._data(result)

            match = re.fullmatch(r"/api/meals/(\d+)/comments", path)
            if method == "POST" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                meal_id = _positive_int(match.group(1), "meal_id")
                payload = self._read_json()
                if set(payload) != {"text"}:
                    raise ValueError("Comment must contain only text")
                text = _bounded_text(payload["text"], "text", required=True)
                result = store.add_comment(
                    trainer_id, meal_id, text,
                    idempotency_key=self._idempotency_key(self.headers),
                )
                return 201, self._data(result)

            match = re.fullmatch(r"/api/meals/(\d+)/history", path)
            if method == "GET" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                return 200, self._data(store.get_meal_history(
                    trainer_id, _positive_int(match.group(1), "meal_id")
                ))

            match = re.fullmatch(r"/api/clients/(\d+)/norms-plan", path)
            if method == "GET" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                return 200, self._data(store.get_norms_plan(
                    trainer_telegram_id=trainer_id,
                    client_id=_positive_int(match.group(1), "client_id"),
                ))

            match = re.fullmatch(r"/api/clients/(\d+)/norms-plan/xlsx/preview", path)
            if method == "POST" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                content = self._read_binary(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    MAX_FILE_BYTES,
                )
                rows = [
                    {
                        "effective_from": row.effective_from.isoformat(),
                        "calories": row.calories,
                        "protein_g": row.protein_g,
                        "fat_g": row.fat_g,
                        "carbs_g": row.carbs_g,
                        "water_ml": row.water_ml,
                        "source_row": row.source_row,
                    }
                    for row in parse_plan(content)
                ]
                return 200, self._data(store.create_plan_preview(
                    trainer_telegram_id=trainer_id,
                    client_id=_positive_int(match.group(1), "client_id"), rows=rows,
                ))

            match = re.fullmatch(r"/api/clients/(\d+)/norms-plan/xlsx/commit", path)
            if method == "POST" and match:
                trainer_id, _, _ = self._authenticate(trainer=True)
                payload = self._read_json()
                if set(payload) != {"upload_token"}:
                    raise ValueError("Некорректное подтверждение плана")
                return 200, self._data(store.commit_plan_preview(
                    trainer_telegram_id=trainer_id,
                    client_id=_positive_int(match.group(1), "client_id"),
                    upload_token=str(payload["upload_token"]),
                ))

            if path == "/api/trainer/code" and method == "GET":
                trainer_id, _, _ = self._authenticate(trainer=True)
                code = store.get_or_create_trainer_code(trainer_telegram_id=trainer_id)
                return 200, self._data({
                    "code": code["code"], "created_at": code["created_at"],
                    "invite_kind": code["invite_kind"],
                })

            if path == "/api/trainer/code/replace" and method == "POST":
                trainer_id, _, _ = self._authenticate(trainer=True)
                code = store.replace_trainer_code(trainer_telegram_id=trainer_id)
                return 200, self._data({
                    "code": code["code"], "created_at": code["created_at"],
                    "invite_kind": code["invite_kind"],
                })

            if path == "/api/me/profile" and method == "GET":
                actor, _, _ = self._authenticate()
                return 200, self._data(store.get_profile(telegram_id=actor))

            if path == "/api/me/profile" and method == "PATCH":
                actor, _, _ = self._authenticate()
                return 200, self._data(store.update_profile(
                    telegram_id=actor, updates=self._read_json(),
                    expected_version=self._expected_version(self.headers),
                ))

            if path == "/api/me/reminders" and method == "GET":
                actor, _, _ = self._authenticate()
                return 200, self._data(store.get_client_reminder_preferences(
                    telegram_id=actor
                ))

            if path == "/api/me/reminders" and method == "PATCH":
                actor, _, _ = self._authenticate()
                return 200, self._data(store.update_client_reminder_preferences(
                    telegram_id=actor,
                    updates=self._read_json(),
                    expected_version=self._expected_version(self.headers),
                    idempotency_key=self._idempotency_key(self.headers),
                ))

            if path == "/api/trainer/reminders" and method == "GET":
                actor, _, _ = self._authenticate(trainer=True)
                return 200, self._data(store.get_trainer_reminder_preferences(
                    trainer_telegram_id=actor
                ))

            if path == "/api/trainer/reminders" and method == "PATCH":
                actor, _, _ = self._authenticate(trainer=True)
                return 200, self._data(store.update_trainer_reminder_preferences(
                    trainer_telegram_id=actor,
                    updates=self._read_json(),
                    expected_version=self._expected_version(self.headers),
                    idempotency_key=self._idempotency_key(self.headers),
                ))

            if path == "/api/me/day" and method == "GET":
                actor, _, _ = self._authenticate()
                value = _parse_iso_date(query.get("date", [""])[0], "date")
                return 200, self._data(store.get_own_day(
                    client_telegram_id=actor, local_date=value
                ))

            if path == "/api/me/summary" and method == "GET":
                actor, _, _ = self._authenticate()
                start, end = self._date_range(query)
                return 200, self._data(store.get_own_summary(
                    client_telegram_id=actor, date_from=start, date_to=end
                ))

            if path == "/api/me/meals" and method == "GET":
                actor, _, _ = self._authenticate()
                start, end = self._date_range(query)
                result = store.list_own_meals(
                    client_telegram_id=actor, date_from=start, date_to=end,
                    cursor=query.get("cursor", [None])[0],
                    limit=int(query.get("limit", [20])[0]),
                )
                return 200, self._data(result["items"], next_cursor=result["next_cursor"])

            if path == "/api/me/meals/drafts" and method == "POST":
                actor, _, _ = self._authenticate()
                payload = self._read_json()
                allowed = {"source", "eaten_at", "meal_type", "note", "hunger_level", "mood", "items"}
                if set(payload) - allowed:
                    raise ValueError("Черновик содержит лишние поля")
                source = str(payload.get("source", "manual"))
                if source != "manual":
                    raise ValueError("В веб-кабинете создается ручной черновик")
                result = store.create_meal_draft(
                    client_telegram_id=actor, source="manual", eaten_at=payload.get("eaten_at", ""),
                    meal_type=str(payload.get("meal_type", "")), note=str(payload.get("note", "")),
                    hunger_level=payload.get("hunger_level"), mood=payload.get("mood"),
                    items=self._prepare_items(payload.get("items")),
                    idempotency_key=self._idempotency_key(self.headers),
                )
                return 201, self._data(result)

            match = re.fullmatch(r"/api/me/meals/(\d+)", path)
            if method == "GET" and match:
                actor, _, _ = self._authenticate()
                return 200, self._data(store.get_own_meal(
                    client_telegram_id=actor,
                    meal_id=_positive_int(match.group(1), "meal_id"),
                ))
            if method == "PATCH" and match:
                actor, _, _ = self._authenticate()
                payload = self._read_json()
                if "items" in payload:
                    payload["items"] = self._prepare_items(payload["items"])
                return 200, self._data(store.update_meal_as_client(
                    client_telegram_id=actor, meal_id=_positive_int(match.group(1), "meal_id"),
                    updates=payload, expected_version=self._expected_version(self.headers),
                ))

            match = re.fullmatch(r"/api/me/meals/(\d+)/confirm", path)
            if method == "POST" and match:
                actor, _, _ = self._authenticate()
                return 200, self._data(store.confirm_meal(
                    client_telegram_id=actor, meal_id=_positive_int(match.group(1), "meal_id"),
                    idempotency_key=self._idempotency_key(self.headers),
                    expected_version=self._expected_version(self.headers),
                ))

            match = re.fullmatch(r"/api/me/meals/(\d+)/cancel", path)
            if method == "POST" and match:
                actor, _, _ = self._authenticate()
                return 200, self._data(store.cancel_meal(
                    client_telegram_id=actor, meal_id=_positive_int(match.group(1), "meal_id"),
                    expected_version=self._expected_version(self.headers),
                ))

            if path == "/api/me/water" and method == "POST":
                actor, _, _ = self._authenticate()
                payload = self._read_json()
                if set(payload) != {"amount_ml", "logged_at"}:
                    raise ValueError("Некорректная запись воды")
                return 201, self._data(store.add_water(
                    client_telegram_id=actor, amount_ml=payload["amount_ml"],
                    logged_at=payload["logged_at"],
                    idempotency_key=self._idempotency_key(self.headers),
                ))

            match = re.fullmatch(r"/api/me/water/(\d+)", path)
            if method == "GET" and match:
                actor, _, _ = self._authenticate()
                return 200, self._data(store.get_own_water(
                    client_telegram_id=actor,
                    water_id=_positive_int(match.group(1), "water_id"),
                ))
            if method == "PATCH" and match:
                actor, _, _ = self._authenticate()
                payload = self._read_json()
                if set(payload) != {"amount_ml", "logged_at"}:
                    raise ValueError("Некорректная запись воды")
                return 200, self._data(store.update_water(
                    client_telegram_id=actor, water_id=_positive_int(match.group(1), "water_id"),
                    amount_ml=payload["amount_ml"], logged_at=payload["logged_at"],
                    expected_version=self._expected_version(self.headers),
                ))
            if method == "DELETE" and match:
                actor, _, _ = self._authenticate()
                return 200, self._data(store.cancel_water(
                    client_telegram_id=actor, water_id=_positive_int(match.group(1), "water_id"),
                    expected_version=self._expected_version(self.headers),
                ))

            if path == "/api/me/weight" and method == "POST":
                actor, _, _ = self._authenticate()
                payload = self._read_json()
                if set(payload) - {"weight_kg", "measured_at", "note"}:
                    raise ValueError("Некорректная запись веса")
                return 201, self._data(store.add_weight(
                    client_telegram_id=actor, weight_kg=payload.get("weight_kg"),
                    measured_at=payload.get("measured_at", ""), note=payload.get("note", ""),
                    idempotency_key=self._idempotency_key(self.headers),
                ))

            match = re.fullmatch(r"/api/me/weight/(\d+)", path)
            if method == "GET" and match:
                actor, _, _ = self._authenticate()
                return 200, self._data(store.get_own_weight(
                    client_telegram_id=actor,
                    weight_id=_positive_int(match.group(1), "weight_id"),
                ))
            if method == "PATCH" and match:
                actor, _, _ = self._authenticate()
                payload = self._read_json()
                if set(payload) - {"weight_kg", "measured_at", "note"}:
                    raise ValueError("Некорректная запись веса")
                return 200, self._data(store.update_weight(
                    client_telegram_id=actor, weight_id=_positive_int(match.group(1), "weight_id"),
                    weight_kg=payload.get("weight_kg"), measured_at=payload.get("measured_at", ""),
                    note=payload.get("note", ""),
                    expected_version=self._expected_version(self.headers),
                ))
            if method == "DELETE" and match:
                actor, _, _ = self._authenticate()
                return 200, self._data(store.cancel_weight(
                    client_telegram_id=actor, weight_id=_positive_int(match.group(1), "weight_id"),
                    expected_version=self._expected_version(self.headers),
                ))

            if path == "/api/me/norms" and method == "GET":
                actor, _, _ = self._authenticate()
                on_date = _parse_iso_date(query.get("on", [""])[0], "on")
                return 200, self._data(store.get_own_norms(
                    client_telegram_id=actor, on_date=on_date
                ))

            if path == "/api/me/trainer" and method == "GET":
                actor, _, _ = self._authenticate()
                return 200, self._data(store.get_trainer_for_client(client_telegram_id=actor))

            if path == "/api/me/trainer/link" and method == "POST":
                actor, _, _ = self._authenticate()
                payload = self._read_json()
                if set(payload) != {"code"}:
                    raise ValueError("Укажите код тренера")
                return 200, self._data(store.link_client_by_code(
                    client_telegram_id=actor, code=str(payload["code"])
                ))

            if path == "/api/me/trainer/link" and method == "DELETE":
                actor, _, _ = self._authenticate()
                store.unlink_trainer(client_telegram_id=actor)
                return 200, self._data({"ok": True})

            match = re.fullmatch(r"/api/clients/(\d+)/link", path)
            if method == "DELETE" and match:
                actor, _, _ = self._authenticate(trainer=True)
                store.unlink_client(
                    trainer_telegram_id=actor,
                    client_id=_positive_int(match.group(1), "client_id"),
                )
                return 200, self._data({"ok": True})

            match = re.fullmatch(r"/api/clients/(\d+)/chat", path)
            if method == "GET" and match:
                actor, _, _ = self._authenticate(trainer=True)
                username = store.get_client_chat_username(
                    trainer_telegram_id=actor,
                    client_id=_positive_int(match.group(1), "client_id"),
                )
                return 200, self._data({"url": f"https://t.me/{username}"})

            if path == "/api/norms-plan/template.xlsx" and method == "GET":
                self._authenticate()
                return 200, build_template().getvalue(), (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ), "nutrition-norms-template.xlsx"

            match = re.fullmatch(r"/api/meals/(\d+)/photo", path)
            if method == "GET" and match:
                actor, _, _ = self._authenticate(trainer=True)
                file_id = store.get_meal_photo_file_id(
                    actor_telegram_id=actor,
                    meal_id=_positive_int(match.group(1), "meal_id"), trainer=True,
                )
                if not file_id or config.photo_loader is None:
                    raise PermissionError("Фото не найдено")
                content_type, content = config.photo_loader(file_id)
                return 200, content, content_type, None

            match = re.fullmatch(r"/api/me/meals/(\d+)/photo", path)
            if method == "GET" and match:
                actor, _, _ = self._authenticate()
                file_id = store.get_meal_photo_file_id(
                    actor_telegram_id=actor,
                    meal_id=_positive_int(match.group(1), "meal_id"), trainer=False,
                )
                if not file_id or config.photo_loader is None:
                    raise PermissionError("Фото не найдено")
                content_type, content = config.photo_loader(file_id)
                return 200, content, content_type, None

            if path == "/api/export" and method == "GET":
                scope = query.get("scope", ["self"])[0]
                start, end = self._date_range(query)
                export_format = query.get("format", ["csv"])[0]
                meals: list[dict[str, Any]] = []
                cursor: int | None = None
                if scope == "self":
                    actor, _, _ = self._authenticate()
                    profile = store.get_profile(telegram_id=actor)
                    title = f"Дневник питания: {profile['display_name']}"
                    summary = store.get_own_summary(
                        client_telegram_id=actor, date_from=start, date_to=end
                    )
                    client_id = None
                elif scope == "client":
                    actor, _, _ = self._authenticate(trainer=True)
                    client_id = _positive_int(query.get("client_id", [""])[0], "client_id")
                    summary = store.get_client_summary(actor, client_id, start, end)
                    title = f"Дневник питания: {summary['client']['display_name']}"
                else:
                    raise ValueError("Неизвестная область экспорта")
                while True:
                    if scope == "self":
                        page = store.list_own_meals(
                            client_telegram_id=actor, date_from=start, date_to=end,
                            cursor=cursor, limit=100,
                        )
                    else:
                        page = store.list_client_meals(
                            actor, client_id, date_from=start, date_to=end,
                            cursor=cursor, limit=100,
                        )
                    meals.extend(page["items"])
                    cursor = page["next_cursor"]
                    if cursor is None:
                        break
                    if len(meals) > 5000:
                        raise ValueError("Слишком много записей для одного экспорта")
                period = f"Период: {start} - {end}"
                if export_format == "csv":
                    return 200, build_csv(meals, summary), "text/csv; charset=utf-8", "nutrition.csv"
                if export_format == "xlsx":
                    return 200, build_xlsx(meals, summary), (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ), "nutrition.xlsx"
                if export_format == "print":
                    return 200, build_print_html(
                        meals, title=title, period=period, summary=summary
                    ), (
                        "text/html; charset=utf-8"
                    ), None
                raise ValueError("Неизвестный формат экспорта")

            if path.startswith("/api/reference/"):
                actor, _, _ = self._authenticate()
                del actor
                if reference is None:
                    raise RuntimeError("reference_unavailable")
                if path == "/api/reference/foods" and method == "GET":
                    limit = max(1, min(int(query.get("limit", [10])[0]), 20))
                    return 200, self._data([
                        asdict(item) for item in reference.search(query.get("q", [""])[0], limit)
                    ])
                match = re.fullmatch(r"/api/reference/foods/(\d+)", path)
                if method == "GET" and match:
                    return 200, self._data(asdict(reference.get(match.group(1))))
                if path == "/api/reference/calculate" and method == "POST":
                    payload = self._read_json()
                    if set(payload) != {"fdc_id", "grams"}:
                        raise ValueError("Укажите продукт и массу")
                    return 200, self._data(asdict(reference.calculate(
                        payload["fdc_id"], payload["grams"]
                    )))

            return 404, {"error": {"code": "not_found", "message": "Данные не найдены", "fields": {}}}

        def _handle_api(self, method: str) -> None:
            try:
                result = self._dispatch(method)
                if len(result) == 4:
                    status, body, content_type, filename = result
                    self._send_bytes(status, body, content_type, filename=filename)
                else:
                    status, payload = result
                    self._send_json(status, payload)
            except InitDataError:
                self._error(401, "Откройте кабинет заново из бота")
            except TrainerNotAllowedError:
                self._error(403, "Нет доступа к кабинету тренера")
            except (PermissionError, KeyError):
                # Same response for missing and unauthorized objects prevents id probing.
                self._error(404, "Данные не найдены")
            except (PlanValidationError, FoodReferenceError) as exc:
                self._error(422, str(exc))
            except RuntimeError as exc:
                if str(exc) == "stale_version":
                    self._send_json(409, {"error": {"code": "stale_version", "message": "Запись уже изменилась. Обновите страницу", "fields": {}}})
                elif str(exc) == "idempotency_conflict":
                    self._send_json(409, {"error": {"code": "idempotency_conflict", "message": "Этот запрос уже был сохранен с другими данными", "fields": {}}})
                else:
                    self._error(500, "Не удалось выполнить запрос, попробуйте позже")
            except ValueError as exc:
                if str(exc) == "Прием пищи не найден":
                    self._error(404, "Данные не найдены")
                else:
                    message = str(exc)
                    safe = (
                        message
                        if message and len(message) <= 300 and not any(
                            marker in message.lower()
                            for marker in (
                                "traceback", "sqlite", "select ", "insert ",
                                "update ", "token",
                            )
                        )
                        else "Проверьте введенные значения"
                    )
                    self._error(422, safe)
            except Exception:
                self._error(500, "Не удалось выполнить запрос, попробуйте позже")

        def _serve_html(self) -> None:
            try:
                template = config.html_path.read_text(encoding="utf-8")
            except OSError:
                self._error(500, "Dashboard asset is unavailable")
                return
            nonce = secrets.token_urlsafe(18)
            body = template.replace("__CSP_NONCE__", nonce).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers(nonce=nonce)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path in {"/", "/nutrition", "/login"}:
                self._serve_html()
            elif urlsplit(self.path).path.startswith("/api/"):
                self._handle_api("GET")
            else:
                self._error(404, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            self._handle_api("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle_api("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle_api("DELETE")

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not config.allowed_origin or not self._origin_allowed():
                self._error(403, "Origin is not allowed")
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                f"Content-Type, {INIT_DATA_HEADER}, X-CSRF-Token, Idempotency-Key, If-Match",
            )
            self.send_header("Access-Control-Max-Age", "600")
            self._security_headers()
            self.end_headers()

    return DashboardHandler


class NutritionDashboardServer:
    """Small in-process HTTP server with idempotent lifecycle methods."""

    def __init__(
        self,
        store: Any,
        *,
        bot_token: str,
        allowed_trainer_ids: Iterable[int] | None = None,
        host: str = "127.0.0.1",
        port: int = 8080,
        auth_ttl_seconds: int = DEFAULT_AUTH_TTL_SECONDS,
        future_skew_seconds: int = DEFAULT_FUTURE_SKEW_SECONDS,
        allowed_origin: str | None = None,
        html_path: str | Path | None = None,
        now: Callable[[], int] | None = None,
        session_ttl_seconds: int = 43_200,
        photo_loader: Callable[[str], tuple[str, bytes]] | None = None,
        secure_cookies: bool = True,
    ) -> None:
        allowlist = (
            trainer_allowlist_from_env()
            if allowed_trainer_ids is None
            else {int(item) for item in allowed_trainer_ids}
        )
        if not bot_token:
            raise ValueError("bot_token is required")
        if not allowlist or any(item <= 0 for item in allowlist):
            raise ValueError("A non-empty trainer allowlist is required")
        if not (0 <= int(port) <= 65_535):
            raise ValueError("Invalid dashboard port")
        if not (300 <= int(session_ttl_seconds) <= 86_400):
            raise ValueError("Invalid dashboard session TTL")
        if not secure_cookies and host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Insecure preview cookies are allowed only on loopback")
        if allowed_origin:
            origin = urlsplit(allowed_origin)
            loopback_http = origin.scheme == "http" and origin.hostname in {"127.0.0.1", "localhost"}
            if (
                (origin.scheme != "https" and not loopback_http)
                or not origin.netloc
                or origin.path not in {"", "/"}
            ):
                raise ValueError("Dashboard origin must be an HTTPS origin without a path")
            allowed_origin = f"{origin.scheme}://{origin.netloc}"
            if not secure_cookies and not loopback_http:
                raise ValueError("Insecure preview cookies require a loopback HTTP origin")

        config = DashboardConfig(
            bot_token=bot_token,
            allowed_trainer_ids=frozenset(allowlist),
            auth_ttl_seconds=auth_ttl_seconds,
            future_skew_seconds=future_skew_seconds,
            allowed_origin=allowed_origin,
            html_path=Path(html_path) if html_path else Path(__file__).with_name("nutrition_dashboard.html"),
            now=now or (lambda: int(time.time())),
            session_ttl_seconds=session_ttl_seconds,
            photo_loader=photo_loader or (lambda file_id: _telegram_photo_loader(bot_token, file_id)),
            secure_cookies=secure_cookies,
        )
        self._httpd = _DashboardHTTPServer((host, int(port)), _handler_factory(store, config))
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="nutrition-dashboard",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            self._httpd.shutdown()
            self._thread.join(timeout=5)
        self._httpd.server_close()
        self._thread = None


def create_dashboard_from_env(store: Any, *, bot_token: str) -> NutritionDashboardServer:
    """Build the server for use from python-telegram-bot post_init/post_shutdown."""

    return NutritionDashboardServer(
        store,
        bot_token=bot_token,
        host=os.getenv("NUTRITION_DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("NUTRITION_DASHBOARD_PORT", "8080")),
        auth_ttl_seconds=int(
            os.getenv("NUTRITION_DASHBOARD_AUTH_TTL_SECONDS", str(DEFAULT_AUTH_TTL_SECONDS))
        ),
        future_skew_seconds=int(
            os.getenv("NUTRITION_DASHBOARD_FUTURE_SKEW_SECONDS", str(DEFAULT_FUTURE_SKEW_SECONDS))
        ),
        session_ttl_seconds=int(
            os.getenv("NUTRITION_DASHBOARD_SESSION_TTL_SECONDS", "43200")
        ),
        allowed_origin=os.getenv("NUTRITION_DASHBOARD_ORIGIN") or None,
    )
