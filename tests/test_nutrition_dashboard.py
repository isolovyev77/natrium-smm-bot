import hashlib
import hmac
import io
import json
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlencode

import pytest

from src.nutrition_dashboard import (
    DashboardConfig,
    INIT_DATA_HEADER,
    InitDataError,
    TrainerNotAllowedError,
    _handler_factory,
    verify_telegram_init_data,
)


BOT_TOKEN = "123456:test-token"
NOW = 2_000_000_000
TRAINER_ID = 101


def signed_init_data(user_id=TRAINER_ID, auth_date=NOW, extra=None):
    fields = {
        "auth_date": str(auth_date),
        "query_id": "query-1",
        "user": json.dumps(
            {"id": user_id, "first_name": "Trainer"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    fields.update(extra or {})
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeStore:
    def __init__(self):
        self.norm_calls = []
        self.meal_calls = []
        self.comment_calls = []
        self.fail_clients = None

    def list_trainer_clients(self, trainer_id):
        if self.fail_clients:
            raise RuntimeError(self.fail_clients)
        assert trainer_id == TRAINER_ID
        return [{"id": 10, "telegram_id": 303, "display_name": "</script><script>alert(1)</script>", "timezone": "Europe/Moscow"}]

    def get_client_day(self, trainer_id, client_id, local_date):
        assert trainer_id == TRAINER_ID
        if client_id != 10:
            raise PermissionError("private client detail")
        return {
            "client": {"id": 10, "display_name": "Client"},
            "date": local_date,
            "norms": None,
            "meals": [],
            "water_ml": 0,
            "totals": {"calories": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0},
        }

    def get_client_week(self, trainer_id, client_id, week_start):
        if client_id != 10:
            raise PermissionError("private client detail")
        return {"client": {"id": 10}, "week_start": week_start, "days": [], "averages": {}}

    def update_meal_as_trainer(self, trainer_id, meal_id, updates, *, expected_version=None):
        if meal_id != 21:
            raise PermissionError("private meal detail")
        self.meal_calls.append((trainer_id, meal_id, updates, expected_version))
        return {"id": meal_id, **updates}

    def set_norms(self, trainer_id, client_id, norms, effective_from):
        if client_id != 10:
            raise PermissionError("private client detail")
        self.norm_calls.append((trainer_id, client_id, norms, effective_from))
        return {"client_user_id": client_id, **norms, "effective_from": effective_from}

    def add_comment(self, trainer_id, meal_id, text, *, idempotency_key=None):
        if meal_id != 21:
            raise PermissionError("private meal detail")
        self.comment_calls.append((trainer_id, meal_id, text, idempotency_key))
        return {"meal_id": meal_id, "text": text}


def request(store, method, path, *, init_data=None, payload=None, headers=None, allowed_ids=(TRAINER_ID,)):
    body = b"" if payload is None else json.dumps(payload).encode()
    request_headers = dict(headers or {})
    if init_data is not None:
        request_headers[INIT_DATA_HEADER] = init_data
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(body))
    config = DashboardConfig(
        bot_token=BOT_TOKEN,
        allowed_trainer_ids=frozenset(allowed_ids),
        html_path=Path(__file__).parents[1] / "src" / "nutrition_dashboard.html",
        now=lambda: NOW,
    )
    handler_type = _handler_factory(store, config)
    handler = handler_type.__new__(handler_type)
    handler.command = method
    handler.path = path
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.headers = BytesParser().parsebytes(
        b"".join(f"{key}: {value}\r\n".encode() for key, value in request_headers.items()) + b"\r\n"
    )
    getattr(handler, f"do_{method}")()
    raw_response = handler.wfile.getvalue()
    head, response_body = raw_response.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    response_headers = {
        key.decode(): value.decode().strip()
        for key, value in (line.split(b":", 1) for line in lines[1:])
    }
    return status, response_headers, response_body


def test_verify_init_data_accepts_only_fresh_signed_allowlisted_user():
    user = verify_telegram_init_data(
        signed_init_data(), BOT_TOKEN, {TRAINER_ID}, now=NOW
    )
    assert user["id"] == TRAINER_ID

    with pytest.raises(TrainerNotAllowedError):
        verify_telegram_init_data(
            signed_init_data(user_id=202), BOT_TOKEN, {TRAINER_ID}, now=NOW
        )


@pytest.mark.parametrize(
    "init_data",
    [
        signed_init_data(auth_date=NOW - 601),
        signed_init_data(auth_date=NOW + 31),
        signed_init_data()[:-1] + "0",
        signed_init_data() + f"&auth_date={NOW}",
    ],
)
def test_verify_init_data_rejects_expired_future_bad_signature_and_duplicates(init_data):
    with pytest.raises(InitDataError):
        verify_telegram_init_data(init_data, BOT_TOKEN, {TRAINER_ID}, now=NOW)


def test_api_uses_signed_user_and_escapes_html_metacharacters():
    status, headers, raw = request(
        FakeStore(), "GET", "/api/clients", init_data=signed_init_data()
    )
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert b"<script>" not in raw
    assert b"\\u003c/script\\u003e" in raw
    assert json.loads(raw)["clients"][0]["display_name"].startswith("</script>")


def test_api_auth_errors_are_generic_and_do_not_call_store():
    store = FakeStore()
    missing = request(store, "GET", "/api/clients")
    forged = request(
        store,
        "GET",
        "/api/clients",
        init_data=signed_init_data()[:-1] + "0",
    )
    assert missing[0] == forged[0] == 401
    assert json.loads(missing[2])["error"]["code"] == "authentication_required"
    assert json.loads(forged[2])["error"]["code"] == "authentication_required"


def test_valid_but_non_allowlisted_trainer_is_forbidden():
    status, _, raw = request(
        FakeStore(),
        "GET",
        "/api/clients",
        init_data=signed_init_data(user_id=202),
    )
    assert status == 403
    assert json.loads(raw)["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/api/clients/999/day?date=2033-05-18", None),
        ("PATCH", "/api/meals/999", {"meal_type": "Обед"}),
        ("POST", "/api/meals/999/comments", {"text": "Комментарий"}),
    ],
)
def test_foreign_client_and_meal_ids_have_same_not_found_response(method, path, payload):
    status, _, raw = request(
        FakeStore(), method, path, init_data=signed_init_data(), payload=payload,
        headers=(
            {"If-Match": "1"} if method == "PATCH"
            else {"Idempotency-Key": "comment-test"} if method == "POST" else None
        ),
    )
    assert status == 404
    assert json.loads(raw)["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {"effective_from": "2033-05-18", "calories": -1},
        {"effective_from": "wrong", "calories": 2000},
        {"effective_from": "2033-05-18", "trainer_telegram_id": TRAINER_ID},
        {"effective_from": "2033-05-18"},
    ],
)
def test_bad_norms_are_rejected_before_store_mutation(payload):
    store = FakeStore()
    status, _, _ = request(
        store,
        "POST",
        "/api/clients/10/norms",
        init_data=signed_init_data(),
        payload=payload,
    )
    assert status == 422
    assert store.norm_calls == []


def test_meal_update_passes_only_validated_store_contract():
    store = FakeStore()
    payload = {
        "meal_type": "Обед",
        "note": "Исправлено тренером",
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
    }
    status, _, _ = request(
        store,
        "PATCH",
        "/api/meals/21",
        init_data=signed_init_data(),
        payload=payload,
        headers={"If-Match": "4"},
    )
    assert status == 200
    assert store.meal_calls[0][0:2] == (TRAINER_ID, 21)
    assert store.meal_calls[0][2]["items"][0]["weight_g"] == 180.0
    assert store.meal_calls[0][3] == 4


def test_internal_exception_does_not_leak_details():
    store = FakeStore()
    store.fail_clients = "secret database path and token"
    status, _, raw = request(
        store, "GET", "/api/clients", init_data=signed_init_data()
    )
    assert status == 500
    assert json.loads(raw)["error"]["code"] == "internal_error"
    assert b"secret" not in raw


def test_html_uses_nonce_relative_api_and_text_content_only():
    status, headers, raw = request(FakeStore(), "GET", "/")
    html = raw.decode()
    assert status == 200
    assert "__CSP_NONCE__" not in html
    assert "Content-Security-Policy" in headers
    assert 'api("/api/clients")' in html
    assert ".textContent" in html
    assert ".innerHTML" not in html
