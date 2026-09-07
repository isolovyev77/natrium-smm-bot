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
import json
import math
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qs, parse_qsl, urlsplit


INIT_DATA_HEADER = "X-Telegram-Init-Data"
DEFAULT_AUTH_TTL_SECONDS = 600
DEFAULT_FUTURE_SKEW_SECONDS = 30
MAX_INIT_DATA_BYTES = 16_384
MAX_JSON_BODY_BYTES = 65_536
MAX_TEXT_LENGTH = 2_000
MAX_NUMERIC_VALUE = 100_000.0  # Technical abuse bound, not a nutrition norm.


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
    allowed_trainer_ids: Iterable[int],
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

    allowlist = {int(item) for item in allowed_trainer_ids}
    if trainer_id not in allowlist:
        raise TrainerNotAllowedError("Telegram user is not an allowed trainer")
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


class _DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _handler_factory(store: Any, config: DashboardConfig) -> type[BaseHTTPRequestHandler]:
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
                    f"style-src 'nonce-{nonce}'; img-src 'self' data:; "
                    "connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org"
                )
                self.send_header("Content-Security-Policy", policy)
            if config.allowed_origin:
                self.send_header("Access-Control-Allow-Origin", config.allowed_origin)
                self.send_header("Vary", "Origin")

        def _send_json(self, status: int, payload: Any) -> None:
            body = _safe_json(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._send_json(status, {"error": message})

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or (config.allowed_origin is not None and origin == config.allowed_origin)

        def _authenticate(self) -> int:
            if not self._origin_allowed():
                raise TrainerNotAllowedError("Origin is not allowed")
            init_data = self.headers.get(INIT_DATA_HEADER, "")
            user = verify_telegram_init_data(
                init_data,
                config.bot_token,
                config.allowed_trainer_ids,
                now=config.now(),
                ttl_seconds=config.auth_ttl_seconds,
                future_skew_seconds=config.future_skew_seconds,
            )
            return int(user["id"])

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

        def _dispatch(self, method: str) -> tuple[int, Any]:
            trainer_id = self._authenticate()
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query, keep_blank_values=True)

            if method == "GET" and path == "/api/clients":
                return 200, {"clients": store.list_trainer_clients(trainer_id)}

            match = re.fullmatch(r"/api/clients/(\d+)/day", path)
            if method == "GET" and match:
                client_id = _positive_int(match.group(1), "client_id")
                local_date = _parse_iso_date(query.get("date", [""])[0], "date")
                return 200, store.get_client_day(trainer_id, client_id, local_date)

            match = re.fullmatch(r"/api/clients/(\d+)/week", path)
            if method == "GET" and match:
                client_id = _positive_int(match.group(1), "client_id")
                week_start = _parse_iso_date(query.get("start", [""])[0], "start")
                return 200, store.get_client_week(trainer_id, client_id, week_start)

            match = re.fullmatch(r"/api/clients/(\d+)/norms", path)
            if method == "POST" and match:
                client_id = _positive_int(match.group(1), "client_id")
                norms, effective_from = _validate_norms(self._read_json())
                result = store.set_norms(trainer_id, client_id, norms, effective_from)
                return 200, {"ok": True, "norms": result}

            match = re.fullmatch(r"/api/meals/(\d+)", path)
            if method == "PATCH" and match:
                meal_id = _positive_int(match.group(1), "meal_id")
                updates = _validate_meal_updates(self._read_json())
                result = store.update_meal_as_trainer(trainer_id, meal_id, updates)
                return 200, {"ok": True, "meal": result}

            match = re.fullmatch(r"/api/meals/(\d+)/comments", path)
            if method == "POST" and match:
                meal_id = _positive_int(match.group(1), "meal_id")
                payload = self._read_json()
                if set(payload) != {"text"}:
                    raise ValueError("Comment must contain only text")
                text = _bounded_text(payload["text"], "text", required=True)
                result = store.add_comment(trainer_id, meal_id, text)
                return 201, {"ok": True, "comment": result}

            return 404, {"error": "Not found"}

        def _handle_api(self, method: str) -> None:
            try:
                status, payload = self._dispatch(method)
                self._send_json(status, payload)
            except InitDataError:
                self._error(401, "Откройте кабинет заново из бота")
            except TrainerNotAllowedError:
                self._error(403, "Нет доступа к кабинету тренера")
            except (PermissionError, KeyError):
                # Same response for missing and unauthorized objects prevents id probing.
                self._error(404, "Данные не найдены")
            except ValueError as exc:
                if str(exc) == "Прием пищи не найден":
                    self._error(404, "Данные не найдены")
                else:
                    self._error(400, "Проверьте дату, порцию и значения КБЖУ")
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
            if urlsplit(self.path).path in {"/", "/nutrition"}:
                self._serve_html()
            elif urlsplit(self.path).path.startswith("/api/"):
                self._handle_api("GET")
            else:
                self._error(404, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            self._handle_api("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle_api("PATCH")

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not config.allowed_origin or not self._origin_allowed():
                self._error(403, "Origin is not allowed")
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {INIT_DATA_HEADER}")
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
        if allowed_origin:
            origin = urlsplit(allowed_origin)
            if origin.scheme != "https" or not origin.netloc or origin.path not in {"", "/"}:
                raise ValueError("Dashboard origin must be an HTTPS origin without a path")
            allowed_origin = f"{origin.scheme}://{origin.netloc}"

        config = DashboardConfig(
            bot_token=bot_token,
            allowed_trainer_ids=frozenset(allowlist),
            auth_ttl_seconds=auth_ttl_seconds,
            future_skew_seconds=future_skew_seconds,
            allowed_origin=allowed_origin,
            html_path=Path(html_path) if html_path else Path(__file__).with_name("nutrition_dashboard.html"),
            now=now or (lambda: int(time.time())),
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
        allowed_origin=os.getenv("NUTRITION_DASHBOARD_ORIGIN") or None,
    )
