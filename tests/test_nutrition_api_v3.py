from __future__ import annotations

import hashlib
import hmac
import io
import json
import sqlite3
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlencode

from src.nutrition_dashboard import DashboardConfig, INIT_DATA_HEADER, _handler_factory
from src.nutrition_reference import FoodReference
from src.nutrition_store import NutritionStore


BOT_TOKEN = "123456:test-api-v3"
ORIGIN = "https://nutrition.example"
NOW = 2_000_000_000
TRAINER_1 = 7001
TRAINER_2 = 7002
CLIENT_1 = 8001
CLIENT_2 = 8002


def signed_init_data(user_id: int) -> str:
    fields = {
        "auth_date": str(NOW),
        "query_id": f"query-{user_id}",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def prepared_store(tmp_path: Path) -> tuple[NutritionStore, dict[str, dict]]:
    store = NutritionStore(tmp_path / "nutrition.sqlite3")
    users = {
        "trainer_1": store.ensure_user(telegram_id=TRAINER_1, display_name="Тренер 1"),
        "trainer_2": store.ensure_user(telegram_id=TRAINER_2, display_name="Тренер 2"),
        "client_1": store.ensure_user(telegram_id=CLIENT_1, display_name="Клиент 1"),
        "client_2": store.ensure_user(telegram_id=CLIENT_2, display_name="Клиент 2"),
    }
    for trainer, client in ((TRAINER_1, CLIENT_1), (TRAINER_2, CLIENT_2)):
        code = store.get_or_create_trainer_code(trainer_telegram_id=trainer)["code"]
        store.link_client_by_code(client_telegram_id=client, code=code)
    return store, users


def request(
    store: NutritionStore,
    method: str,
    path: str,
    *,
    init_data: str | None = None,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
    origin: str | None = ORIGIN,
):
    body = b"" if payload is None else json.dumps(payload).encode()
    request_headers = dict(headers or {})
    if origin is not None:
        request_headers["Origin"] = origin
    if init_data is not None:
        request_headers[INIT_DATA_HEADER] = init_data
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(body))
    config = DashboardConfig(
        bot_token=BOT_TOKEN,
        allowed_trainer_ids=frozenset({TRAINER_1, TRAINER_2}),
        allowed_origin=ORIGIN,
        html_path=Path(__file__).parents[1] / "src" / "nutrition_dashboard.html",
        now=lambda: NOW,
        photo_loader=lambda _: ("image/png", b"png"),
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
    raw = handler.wfile.getvalue()
    head, response_body = raw.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    response_headers = {}
    for line in lines[1:]:
        key, value = line.split(b":", 1)
        response_headers[key.decode()] = value.decode().strip()
    return int(lines[0].split()[1]), response_headers, response_body


def cookie_and_csrf(store: NutritionStore, telegram_id: int):
    status, headers, raw = request(
        store, "GET", "/api/session", init_data=signed_init_data(telegram_id)
    )
    assert status == 200
    body = json.loads(raw)["data"]
    return headers["Set-Cookie"].split(";", 1)[0], body["csrf_token"]


def test_session_issues_secure_cookie_and_returns_same_csrf_after_reload(tmp_path):
    store, _ = prepared_store(tmp_path)
    status, headers, raw = request(
        store, "GET", "/api/session", init_data=signed_init_data(CLIENT_1)
    )
    assert status == 200
    assert "__Host-natrium_session=" in headers["Set-Cookie"]
    assert "Secure" in headers["Set-Cookie"] and "HttpOnly" in headers["Set-Cookie"]
    assert "telegram_id" not in raw.decode()
    first = json.loads(raw)["data"]
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    status, _, raw = request(store, "GET", "/api/session", headers={"Cookie": cookie})
    second = json.loads(raw)["data"]
    assert status == 200 and second["csrf_token"] == first["csrf_token"]


def test_login_exchange_origin_one_time_and_expired_session(tmp_path):
    store, _ = prepared_store(tmp_path)
    login = store.create_browser_login_token(telegram_id=CLIENT_1)
    bad = request(
        store, "POST", "/api/auth/browser/exchange",
        payload={"token": login["token"]}, origin="https://evil.example",
    )
    assert bad[0] == 403
    good = request(
        store, "POST", "/api/auth/browser/exchange", payload={"token": login["token"]}
    )
    assert good[0] == 200
    assert request(
        store, "POST", "/api/auth/browser/exchange", payload={"token": login["token"]}
    )[0] == 404
    cookie = good[1]["Set-Cookie"].split(";", 1)[0]
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE nutrition_web_sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    assert request(store, "GET", "/api/session", headers={"Cookie": cookie})[0] == 401


def test_cookie_csrf_profile_write_and_stale_version(tmp_path):
    store, _ = prepared_store(tmp_path)
    cookie, csrf = cookie_and_csrf(store, CLIENT_1)
    headers = {"Cookie": cookie, "If-Match": "0"}
    assert request(
        store, "PATCH", "/api/me/profile", payload={"height_cm": 170}, headers=headers
    )[0] == 403
    headers["X-CSRF-Token"] = csrf
    saved = request(
        store, "PATCH", "/api/me/profile", payload={"height_cm": 170}, headers=headers
    )
    assert saved[0] == 200 and json.loads(saved[2])["data"]["version"] == 1
    stale = request(
        store, "PATCH", "/api/me/profile", payload={"height_cm": 171}, headers=headers
    )
    assert stale[0] == 409


def test_duplicate_idempotency_does_not_add_second_water_record(tmp_path):
    store, _ = prepared_store(tmp_path)
    cookie, csrf = cookie_and_csrf(store, CLIENT_1)
    headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "Idempotency-Key": "same-water"}
    payload = {"amount_ml": 250, "logged_at": "2026-09-08T10:00:00+03:00"}
    assert request(store, "POST", "/api/me/water", payload=payload, headers=headers)[0] == 201
    assert request(store, "POST", "/api/me/water", payload=payload, headers=headers)[0] == 201
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM water_logs WHERE idempotency_key='same-water'").fetchone()[0] == 1


def test_foreign_meal_photo_export_and_unlinked_client_are_hidden(tmp_path):
    store, users = prepared_store(tmp_path)
    meal = store.create_meal_draft(
        client_telegram_id=CLIENT_2, source="manual", photo_file_id="photo",
        eaten_at="2026-09-08T10:00:00+03:00", meal_type="обед",
        items=[{"name":"Блюдо","weight_g":100,"calories":100,"protein_g":1,"fat_g":2,"carbs_g":3}],
    )
    meal = store.confirm_meal(client_telegram_id=CLIENT_2, meal_id=meal["id"], idempotency_key="m")
    cookie, csrf = cookie_and_csrf(store, TRAINER_1)
    headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "If-Match": str(meal["version"])}
    assert request(store, "GET", f"/api/meals/{meal['id']}", headers={"Cookie": cookie})[0] == 404
    assert request(store, "GET", f"/api/meals/{meal['id']}/photo", headers={"Cookie": cookie})[0] == 404
    export = f"/api/export?scope=client&client_id={users['client_2']['id']}&from=2026-09-01&to=2026-09-08&format=csv"
    assert request(store, "GET", export, headers={"Cookie": cookie})[0] == 404
    own_client_id = users["client_1"]["id"]
    store.unlink_client(trainer_telegram_id=TRAINER_1, client_id=own_client_id)
    assert request(
        store, "GET", f"/api/clients/{own_client_id}/day?date=2026-09-08",
        headers={"Cookie": cookie},
    )[0] == 404


def test_reference_payload_is_rebuilt_server_side(tmp_path):
    store, _ = prepared_store(tmp_path)
    candidate = FoodReference().search("banana", limit=1)[0]
    cookie, csrf = cookie_and_csrf(store, CLIENT_1)
    payload = {
        "source": "manual", "eaten_at": "2026-09-08T10:00:00+03:00",
        "meal_type": "snack", "items": [{
            "fdc_id": candidate.fdc_id, "grams": 150,
            "reference_source": "Поддельный источник", "calories": 999999,
        }],
    }
    status, _, raw = request(
        store, "POST", "/api/me/meals/drafts", payload=payload,
        headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Idempotency-Key": "draft"},
    )
    assert status == 201
    item = json.loads(raw)["data"]["items"][0]
    assert item["calculation_method"] == "reference"
    assert item["reference_source"] != "Поддельный источник"
    assert item["calories"] != 999999


def test_reminder_preferences_routes_are_versioned_and_idempotent(tmp_path):
    store, _ = prepared_store(tmp_path)
    initial = request(
        store, "GET", "/api/me/reminders", init_data=signed_init_data(CLIENT_1)
    )
    assert initial[0] == 200
    assert json.loads(initial[2])["data"]["version"] == 0
    payload = {
        "enabled": True,
        "meal": {"enabled": True, "times": ["08:00"]},
        "quiet_hours": {"start": "22:00", "end": "07:00"},
    }
    headers = {"If-Match": "0", "Idempotency-Key": "prefs-one"}
    first = request(
        store, "PATCH", "/api/me/reminders", init_data=signed_init_data(CLIENT_1),
        payload=payload, headers=headers,
    )
    repeated = request(
        store, "PATCH", "/api/me/reminders", init_data=signed_init_data(CLIENT_1),
        payload=payload, headers=headers,
    )
    assert first[0] == repeated[0] == 200
    assert json.loads(first[2])["data"]["version"] == 1
    assert json.loads(repeated[2])["data"]["version"] == 1
    conflict = request(
        store, "PATCH", "/api/me/reminders", init_data=signed_init_data(CLIENT_1),
        payload={"enabled": False},
        headers={"If-Match": "1", "Idempotency-Key": "prefs-one"},
    )
    assert conflict[0] == 409
    trainer = request(
        store, "GET", "/api/trainer/reminders", init_data=signed_init_data(TRAINER_1)
    )
    assert trainer[0] == 200 and json.loads(trainer[2])["data"]["enabled"] is False


def test_create_draft_and_comment_http_retries_do_not_duplicate(tmp_path):
    store, _ = prepared_store(tmp_path)
    draft_payload = {
        "source": "manual",
        "eaten_at": "2026-09-08T10:00:00+03:00",
        "meal_type": "breakfast",
        "note": "",
        "items": [{
            "name": "Каша", "weight_g": 200, "calories": 250,
            "protein_g": 8, "fat_g": 5, "carbs_g": 45,
        }],
    }
    headers = {"Idempotency-Key": "draft-http-one"}
    first = request(
        store, "POST", "/api/me/meals/drafts", init_data=signed_init_data(CLIENT_1),
        payload=draft_payload, headers=headers,
    )
    second = request(
        store, "POST", "/api/me/meals/drafts", init_data=signed_init_data(CLIENT_1),
        payload=draft_payload, headers=headers,
    )
    assert first[0] == second[0] == 201
    first_id = json.loads(first[2])["data"]["id"]
    assert json.loads(second[2])["data"]["id"] == first_id
    assert request(
        store, "POST", "/api/me/meals/drafts", init_data=signed_init_data(CLIENT_1),
        payload={**draft_payload, "meal_type": "lunch"}, headers=headers,
    )[0] == 409

    comment_headers = {"Idempotency-Key": "comment-http-one"}
    comment = request(
        store, "POST", f"/api/meals/{first_id}/comments",
        init_data=signed_init_data(TRAINER_1), payload={"text": "Хорошо"},
        headers=comment_headers,
    )
    repeated = request(
        store, "POST", f"/api/meals/{first_id}/comments",
        init_data=signed_init_data(TRAINER_1), payload={"text": "Хорошо"},
        headers=comment_headers,
    )
    assert comment[0] == repeated[0] == 201
    assert json.loads(comment[2])["data"]["id"] == json.loads(repeated[2])["data"]["id"]
    assert request(
        store, "POST", f"/api/meals/{first_id}/comments",
        init_data=signed_init_data(TRAINER_1), payload={"text": "Иначе"},
        headers=comment_headers,
    )[0] == 409
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM meals").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM trainer_comments").fetchone()[0] == 1


def test_client_can_soft_cancel_confirmed_meal_with_version_and_audit(tmp_path):
    store, _ = prepared_store(tmp_path)
    draft = store.create_meal_draft(
        client_telegram_id=CLIENT_1, source="manual",
        eaten_at="2026-09-08T10:00:00+03:00", meal_type="lunch",
        items=[{
            "name": "Обед", "weight_g": 300, "calories": 500,
            "protein_g": 25, "fat_g": 15, "carbs_g": 60,
        }],
    )
    meal = store.confirm_meal(
        client_telegram_id=CLIENT_1, meal_id=draft["id"], idempotency_key="confirm-one"
    )
    status, _, raw = request(
        store, "POST", f"/api/me/meals/{meal['id']}/cancel",
        init_data=signed_init_data(CLIENT_1), headers={"If-Match": str(meal["version"])},
    )
    assert status == 200
    cancelled = json.loads(raw)["data"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["version"] == meal["version"] + 1
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='cancel_confirmed' AND entity_id=?",
            (meal["id"],),
        ).fetchone()[0] == 1


def test_self_detail_routes_return_cancelled_own_objects_and_hide_foreign_ids(tmp_path):
    store, _ = prepared_store(tmp_path)
    meal = store.create_meal_draft(
        client_telegram_id=CLIENT_1, source="manual",
        eaten_at="2026-09-08T10:00:00+03:00", meal_type="lunch",
        items=[{
            "name": "Обед", "weight_g": 300, "calories": 500,
            "protein_g": 25, "fat_g": 15, "carbs_g": 60,
        }],
    )
    meal = store.cancel_meal(
        client_telegram_id=CLIENT_1, meal_id=meal["id"],
        expected_version=meal["version"],
    )
    water = store.add_water(
        client_telegram_id=CLIENT_1, amount_ml=250,
        logged_at="2026-09-08T10:00:00+03:00", idempotency_key="detail-water",
    )
    water = store.cancel_water(
        client_telegram_id=CLIENT_1, water_id=water["id"],
        expected_version=water["version"],
    )
    weight = store.add_weight(
        client_telegram_id=CLIENT_1, weight_kg=70,
        measured_at="2026-09-08T10:00:00+03:00", idempotency_key="detail-weight",
    )
    weight = store.cancel_weight(
        client_telegram_id=CLIENT_1, weight_id=weight["id"],
        expected_version=weight["version"],
    )
    own_paths = (
        f"/api/me/meals/{meal['id']}",
        f"/api/me/water/{water['id']}",
        f"/api/me/weight/{weight['id']}",
    )
    for path in own_paths:
        status, _, raw = request(
            store, "GET", path, init_data=signed_init_data(CLIENT_1)
        )
        assert status == 200
        assert json.loads(raw)["data"]["status"] == "cancelled"
        assert request(
            store, "GET", path, init_data=signed_init_data(CLIENT_2)
        )[0] == 404
    for kind in ("meals", "water", "weight"):
        assert request(
            store, "GET", f"/api/me/{kind}/999999",
            init_data=signed_init_data(CLIENT_1),
        )[0] == 404


def test_confirm_rejects_stale_preview_then_accepts_fresh_version_and_replay(tmp_path):
    store, _ = prepared_store(tmp_path)
    draft = store.create_meal_draft(
        client_telegram_id=CLIENT_1, source="manual",
        eaten_at="2026-09-08T10:00:00+03:00", meal_type="lunch",
        items=[{
            "name": "Обед", "weight_g": 300, "calories": 500,
            "protein_g": 25, "fat_g": 15, "carbs_g": 60,
        }],
    )
    preview_version = draft["version"]
    moved = request(
        store, "PATCH", f"/api/me/meals/{draft['id']}",
        init_data=signed_init_data(CLIENT_1),
        headers={"If-Match": str(preview_version)},
        payload={"eaten_at": "2026-09-09T10:00:00+03:00"},
    )
    assert moved[0] == 200
    current = json.loads(moved[2])["data"]
    confirm_headers = {
        "If-Match": str(preview_version),
        "Idempotency-Key": "confirm-after-refresh",
    }
    stale = request(
        store, "POST", f"/api/me/meals/{draft['id']}/confirm",
        init_data=signed_init_data(CLIENT_1), headers=confirm_headers,
    )
    assert stale[0] == 409
    fetched = request(
        store, "GET", f"/api/me/meals/{draft['id']}",
        init_data=signed_init_data(CLIENT_1),
    )
    fetched_meal = json.loads(fetched[2])["data"]
    assert fetched[0] == 200
    assert fetched_meal["version"] == current["version"]
    assert fetched_meal["local_date"] == "2026-09-09"
    confirm_headers["If-Match"] = str(fetched_meal["version"])
    fresh = request(
        store, "POST", f"/api/me/meals/{draft['id']}/confirm",
        init_data=signed_init_data(CLIENT_1), headers=confirm_headers,
    )
    replay = request(
        store, "POST", f"/api/me/meals/{draft['id']}/confirm",
        init_data=signed_init_data(CLIENT_1), headers=confirm_headers,
    )
    assert fresh[0] == replay[0] == 200
    assert json.loads(fresh[2])["data"] == json.loads(replay[2])["data"]


def test_guided_ux_comment_recent_and_repeat_routes(tmp_path):
    store, _ = prepared_store(tmp_path)
    draft = store.create_meal_draft(
        client_telegram_id=CLIENT_1,
        source="manual",
        eaten_at="2026-09-07T08:00:00+03:00",
        meal_type="breakfast",
        mood="good",
        items=[{
            "name": "Каша",
            "weight_g": 250,
            "portion_text": "тарелка",
            "calories": 320,
            "protein_g": 10,
            "fat_g": 8,
            "carbs_g": 52,
        }],
    )
    meal = store.confirm_meal(
        client_telegram_id=CLIENT_1,
        meal_id=draft["id"],
        idempotency_key="guided-confirm",
    )
    store.add_comment(TRAINER_1, meal["id"], "Хороший завтрак")

    comments = request(
        store,
        "GET",
        "/api/me/trainer-comments?limit=5",
        init_data=signed_init_data(CLIENT_1),
    )
    assert comments[0] == 200
    comment = json.loads(comments[2])["data"][0]
    assert comment["text"] == "Хороший завтрак"
    assert comment["meal"] == {
        "id": meal["id"],
        "eaten_at": meal["eaten_at"],
        "local_date": "2026-09-07",
        "timezone": "Europe/Moscow",
        "meal_type": "breakfast",
    }

    recent = request(
        store,
        "GET",
        "/api/me/meals/recent?limit=5",
        init_data=signed_init_data(CLIENT_1),
    )
    assert recent[0] == 200
    assert json.loads(recent[2])["data"][0]["id"] == meal["id"]

    headers = {"Idempotency-Key": "repeat-http-one"}
    payload = {"eaten_at": "2026-09-08T09:30:00+03:00"}
    first = request(
        store,
        "POST",
        f"/api/me/meals/{meal['id']}/repeat",
        init_data=signed_init_data(CLIENT_1),
        payload=payload,
        headers=headers,
    )
    replay = request(
        store,
        "POST",
        f"/api/me/meals/{meal['id']}/repeat",
        init_data=signed_init_data(CLIENT_1),
        payload=payload,
        headers=headers,
    )
    assert first[0] == replay[0] == 201
    repeated = json.loads(first[2])["data"]
    assert json.loads(replay[2])["data"]["id"] == repeated["id"]
    assert repeated["status"] == "draft"
    assert repeated["mood"] is None
    assert repeated["comments"] == []
    assert store.get_own_day(
        client_telegram_id=CLIENT_1, local_date="2026-09-08"
    )["meals"] == []
