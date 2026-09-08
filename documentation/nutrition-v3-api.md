# Natrium Nutrition API v3

Статус: согласованный контракт для клиентского и тренерского кабинетов. Старые
маршруты `/api/clients`, `/api/clients/{id}/day`, `/week`, `/norms`,
`/api/meals/{id}` и `/comments` сохраняются на время перехода.

## Общие правила

- Все даты имеют вид `YYYY-MM-DD`, время имеет ISO 8601 с часовым поясом.
- Идентификаторы `user_id`, `client_id`, `meal_id` внутренние. Telegram ID API
  не возвращает и от клиента не принимает.
- Отсутствующее значение передается как `null`. Ноль не означает отсутствие.
- День рассчитывается в часовом поясе пользователя, сохраненном в профиле.
- Каждый запрос заново проверяет роль и активную связь тренера с клиентом.
- Недоступный и несуществующий чужой объект одинаково отвечает `404`.
- JSON ограничен 64 КиБ. XLSX ограничен 2 МиБ, 366 строками и правилами
  `nutrition_plan.parse_plan`.
- Мутации принимают `Idempotency-Key`. Изменение существующей записи также
  принимает `If-Match` с ее `version`.
- Успех возвращает `{"data": ..., "meta": ...}`. Ошибка возвращает
  `{"error":{"code":"...","message":"...","fields":{}}}`.

Основные коды: `invalid_request` 400, `authentication_required` 401,
`forbidden` 403, `not_found` 404, `stale_version` 409,
`validation_failed` 422, `rate_limited` 429, `internal_error` 500.

## Аутентификация

Mini App передает сырой `Telegram.WebApp.initData` в
`X-Telegram-Init-Data`. Сервер проверяет подпись, `auth_date` и срок. Поля из
`initDataUnsafe` не являются основанием доступа.

Обычный браузер получает от бота одноразовую ссылку вида
`https://<origin>/login#token=<secret>`. Фрагмент не попадает в HTTP-запрос.
Страница отправляет токен в `POST /api/auth/browser/exchange`. Токен хранится
только в виде SHA-256, действует 10 минут и погашается одной транзакцией.
Ответ устанавливает cookie `__Host-natrium_session` с `Secure`, `HttpOnly`,
`SameSite=Lax`, `Path=/`, не более 12 часов, и возвращает CSRF-токен. Для
мутаций cookie-сессии обязателен `X-CSRF-Token`. Выход и отзыв роли немедленно
закрывают сессию; роль и связь все равно проверяются на каждом запросе.
`GET /api/session` возвращает действующий CSRF-токен и после перезагрузки
страницы. Первый такой запрос с валидным initData выдает короткую cookie-сессию;
после истечения initData интерфейс продолжает работать до явного срока cookie,
а затем просит открыть кабинет из бота заново.

### Сессия

`GET /api/session`

```json
{
  "data": {
    "api_version": 3,
    "user": {
      "id": 12,
      "display_name": "Имя",
      "timezone": "Europe/Moscow"
    },
    "roles": ["self", "trainer"],
    "today": "2026-09-08",
    "capabilities": {
      "self_dashboard": true,
      "trainer_dashboard": true,
      "edit_own_diary": true,
      "manage_clients": true,
      "manage_norms": true,
      "xlsx_plan": true,
      "export_csv": true,
      "export_xlsx": true,
      "print_view": true,
      "meal_photo": true,
      "browser_login": true,
      "reminder_preferences": true,
      "trainer_reminders": true
    }
  },
  "meta": {}
}
```

`POST /api/auth/browser/exchange` принимает `{"token":"..."}`.
`POST /api/auth/logout` закрывает текущую cookie-сессию.

## Общие модели

`profile`:

```json
{
  "user_id": 12,
  "display_name": "Имя",
  "timezone": "Europe/Moscow",
  "height_cm": 178.0,
  "goal": "Поддерживать режим",
  "initial_weight_kg": 82.5,
  "version": 3,
  "updated_at": "2026-09-08T06:00:00+00:00"
}
```

`norms` содержит `effective_from`, `calories`, `protein_g`, `fat_g`,
`carbs_g`, `water_ml`. Каждое числовое поле может быть `null`.

`meal` содержит `id`, `eaten_at`, `local_date`, `meal_type`, `note`,
`hunger_level` (`1..10` или `null`), `mood` (`great`, `good`, `neutral`,
`low`, `stressed` или `null`), `status`, `version`, `items`, `comments`.
Фотография представлена только `has_photo: true|false`. Telegram file ID в
JSON не выходит.

`item` содержит `id`, `name`, `weight_g`, `portion_text`, КБЖУ,
`approximate`, `calculation_method`. Для справочной позиции добавляется
`reference` с `fdc_id`, `source`, `version`, `url`, `description`,
`preparation` и снимком `per_100g`. Ручная правка КБЖУ очищает `reference`.

`day`:

```json
{
  "date": "2026-09-08",
  "norms": null,
  "totals": {
    "calories": 0.0,
    "protein_g": 0.0,
    "fat_g": 0.0,
    "carbs_g": 0.0,
    "water_ml": 0
  },
  "weight_kg": null,
  "meals": []
}
```

## Кабинет клиента

- `GET /api/me/profile`
- `PATCH /api/me/profile` с любым подмножеством `display_name`, `timezone`,
  `height_cm`, `goal`, `initial_weight_kg`.
- `GET /api/me/day?date=YYYY-MM-DD`
- `GET /api/me/summary?from=YYYY-MM-DD&to=YYYY-MM-DD&bucket=day`
- `GET /api/me/meals?from=...&to=...&cursor=...&limit=20`
- `POST /api/me/meals/drafts`
- `PATCH /api/me/meals/{meal_id}`
- `POST /api/me/meals/{meal_id}/confirm`
- `POST /api/me/meals/{meal_id}/cancel` с `If-Match` для мягкой отмены
  черновика или подтвержденной записи.
- `POST /api/me/water` с `amount_ml`, `logged_at`.
- `PATCH /api/me/water/{water_id}` и `DELETE /api/me/water/{water_id}` для
  исправления и мягкой отмены с аудитом и версией.
- `POST /api/me/weight` с `weight_kg`, `measured_at`, необязательным `note`.
- `PATCH /api/me/weight/{weight_id}` и `DELETE /api/me/weight/{weight_id}`.
- `GET /api/me/norms?on=YYYY-MM-DD`
- `GET /api/me/trainer`
- `POST /api/me/trainer/link` с многоразовым кодом.
- `DELETE /api/me/trainer/link`

Создание черновика принимает:

```json
{
  "source": "manual",
  "eaten_at": "2026-09-08T09:30:00+03:00",
  "meal_type": "breakfast",
  "note": "",
  "hunger_level": 6,
  "mood": "good",
  "items": []
}
```

Клиент может исправить собственный черновик и собственную подтвержденную
запись. Правка подтвержденной записи создает аудит и повышает `version`.
Создание черновика, подтверждение, комментарий тренера, вода и вес
идемпотентны. Повтор с тем же ключом и другим payload отвечает 409. Ошибочный
прием пищи, вода и вес не удаляются физически: отмена меняет статус, повышает
версию и остается в аудите.

`GET /api/me/summary` возвращает:

```json
{
  "data": {
    "period": {"from":"2026-09-01","to":"2026-09-08","bucket":"day"},
    "totals": {},
    "averages": {},
    "days_with_entries": 6,
    "series": [
      {
        "date":"2026-09-01",
        "calories":null,
        "protein_g":null,
        "fat_g":null,
        "carbs_g":null,
        "water_ml":null,
        "weight_kg":null,
        "meal_count":0,
        "norms": {
          "effective_from":"2026-08-20",
          "calories":2000,
          "protein_g":120,
          "fat_g":70,
          "carbs_g":230,
          "water_ml":2000
        }
      }
    ],
    "top_foods": []
  },
  "meta": {}
}
```

Пустой день сохраняет `null` в рядах питания и воды, чтобы интерфейс не
выдавал отсутствие записи за нулевое потребление. `top_foods` группирует
справочные позиции по `fdc_id`, а несверенные отдельно помечает
`calculation_method`.

Историческая норма возвращается в `series[].norms` для каждого дня. Сервер
берет последнюю норму с `effective_from <= series[].date`, включая норму,
заданную до начала выбранного периода. Это позволяет строить корректную
ступенчатую линию нормы при ее изменениях.

## Кабинет тренера

- `GET /api/clients?cursor=...&limit=50`
- `GET /api/clients/{client_id}/day?date=YYYY-MM-DD`
- `GET /api/clients/{client_id}/week?start=YYYY-MM-DD`
- `GET /api/clients/{client_id}/summary?from=...&to=...&bucket=day`
- `GET /api/clients/{client_id}/meals?from=...&to=...&cursor=...&limit=20`
- `GET /api/meals/{meal_id}`
- `PATCH /api/meals/{meal_id}`
- `GET /api/meals/{meal_id}/history`
- `POST /api/meals/{meal_id}/comments`
- `GET /api/meals/{meal_id}/photo`
- `GET /api/clients/{client_id}/norms-plan`
- `GET /api/norms-plan/template.xlsx`
- `POST /api/clients/{client_id}/norms`
- `POST /api/clients/{client_id}/norms-plan/xlsx/preview`
- `POST /api/clients/{client_id}/norms-plan/xlsx/commit`
- `GET /api/trainer/code`
- `POST /api/trainer/code/replace`
- `DELETE /api/clients/{client_id}/link`
- `GET /api/clients/{client_id}/chat`

Карточка клиента в `/api/clients` содержит `id`, `display_name`, `timezone`,
`local_today`, рассчитанный на сервере в часовом поясе этого клиента,
`last_activity_at`, `last_meal_at`, `today.date` и `today` с КБЖУ и водой,
`norms_status` (`set`, `partial`, `missing`). Пустые значения остаются `null`.
Если сохранен публичный Telegram username, карточка также содержит
`can_open_chat: true` и относительный `chat_url`. Frontend получает этот route
через authenticated fetch; после повторной проверки связи ответ возвращает
публичный `https://t.me/...`, который открывается через Telegram WebApp API.
Telegram ID в ответ не включается.

Шаблон скачивается authenticated fetch-запросом к
`GET /api/norms-plan/template.xlsx` и сохраняется из Blob. XLSX preview
принимает сырое тело с Content-Type
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, не
multipart. Ответ содержит `upload_token`, `expires_at`, полностью проверенные
`rows` и `conflicts` по уже существующим датам. Commit принимает только
`{"upload_token":"..."}`. Preview связан с тренером и клиентом, хранится в
БД в нормализованном виде, действует 15 минут и применяется целиком в одной
транзакции. Смена клиента, отвязка, истечение или повторный commit делают токен
недействительным.

## Справочник, фото и экспорт

- `GET /api/reference/foods?q=...&limit=10`
- `GET /api/reference/foods/{fdc_id}`
- `POST /api/reference/calculate` с `fdc_id`, `grams`
- `GET /api/export?scope=self&from=...&to=...&format=csv|xlsx|print`
- `GET /api/export?scope=client&client_id=...&from=...&to=...&format=csv|xlsx|print`

Справочник работает offline и возвращает источник, версию и снимок на 100 г.
Он не приписывает USDA-происхождение AI-оценке. Фото выдается отдельным
маршрутом после проверки владельца или активной связи тренера, с `no-store` и
без возврата Telegram file ID.
Frontend передает только `fdc_id` и граммы. Сервер сам читает локальную запись,
пересчитывает КБЖУ и формирует provenance; входные `source`, `version`, URL и
снимок от браузера игнорируются. Фото и экспорт доступны владельцу записи либо
тренеру с действующей связью и никогда не содержат Telegram file ID, Telegram
ID, токены или allowlist.

CSV и XLSX ограничены периодом 366 дней. `print` возвращает безопасную HTML
версию для печати браузером; генерация PDF на сервере в первый этап не входит.

## Напоминания

Все настройки по умолчанию выключены. Время задается в локальном времени, а
часовой пояс всегда читается из текущего профиля пользователя. Изменение
часового пояса или настроек отменяет еще не отправленные задания старой версии.

- `GET /api/me/reminders`
- `PATCH /api/me/reminders` с обязательными `If-Match` и `Idempotency-Key`
- `GET /api/trainer/reminders`
- `PATCH /api/trainer/reminders` с обязательными `If-Match` и
  `Idempotency-Key`

Клиентские настройки:

```json
{
  "version": 1,
  "enabled": true,
  "meal": {"enabled": true, "times": ["08:30", "14:00"], "max_times": 3},
  "water": {
    "enabled": true,
    "mode": "interval",
    "times": [],
    "interval_minutes": 120,
    "start_local": "09:00",
    "end_local": "21:00"
  },
  "quiet_hours": {"start": "22:00", "end": "07:00"},
  "timezone": "Europe/Moscow",
  "updated_at": "2026-09-08T06:00:00+00:00"
}
```

Разрешено не больше трех времен еды и двенадцати явных времен воды. Интервал
воды составляет 60–720 минут и работает внутри возрастающих границ дня. Тихие
часы могут проходить через полночь.

Настройки тренера:

```json
{
  "version": 1,
  "enabled": true,
  "daily_digest": {
    "enabled": true,
    "time": "20:00",
    "inactivity_days": 2,
    "calorie_comparison_enabled": true,
    "over_plan_percent": 10
  },
  "timezone": "Europe/Moscow",
  "updated_at": "2026-09-08T06:00:00+00:00"
}
```

Сводка тренера проверяет только текущих связанных клиентов. Формулировки
нейтральны: отсутствие записей за заданное число дней и сравнение с явно
заданной нормой. Проверка «не ел больше N часов» не используется.

Очередь имеет устойчивую дедупликацию по получателю, типу, локальной дате и
слоту. Планировщик берет только задания в свежем окне до 10 минут, поэтому
после рестарта не возникает серия старых сообщений. Перед отправкой он заново
проверяет opt-in, версию настроек, роль тренера и активные связи. Тесты работают
только с поддельным sender и не отправляют сообщения пользователям.
Процесс планировщика запускается только при явном
`NUTRITION_REMINDERS_ENABLED=1`; пользовательские настройки при этом все равно
остаются выключенными, пока сам пользователь их не сохранит.

## Миграции

Схема 3 добавляет `nutrition_profiles`, `weight_logs`, поля
`hunger_level`, `mood`, `version` в `meals`, а также
`nutrition_web_login_tokens`, `nutrition_web_sessions` и
`nutrition_plan_previews`; вода и вес получают версию и мягкую отмену. Все
миграции идут в явной транзакции и не меняют
существующие строки питания, нормы, комментарии, связи и согласия.

Схема 4 добавляет клиентские и тренерские настройки напоминаний,
`nutrition_notification_outbox` и таблицу устойчивой дедупликации двух create
операций API. Она только opt-in, учитывает часовой пояс и тихие часы.
Тарифы и платежи в API не входят.

## Размещение

Приложение слушает только `127.0.0.1:8080`. Публичный origin обязан быть HTTPS
и совпадать с `NUTRITION_DASHBOARD_ORIGIN`; CORS для другого origin не нужен.
В production уже установлен Tailscale с MagicDNS, а Tailscale Serve/Funnel пока
не настроены. Предпочтительный путь без покупки домена: стабильный HTTPS
`*.ts.net` через Tailscale Funnel к loopback-порту. До включения нужно проверить,
что Funnel разрешен политикой tailnet, закрепить origin в Telegram и в `.env`,
и добавить отдельную проверку доступности. Tailscale Serve годится только для
участников tailnet и поэтому не подходит клиентам клуба.

Прямой reverse proxy на системном 443 сейчас невозможен без переноса
существующего сервиса, который уже занимает этот порт. Временный случайный
туннель не подходит для постоянной кнопки Mini App.
