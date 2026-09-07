BEGIN TRANSACTION;
CREATE TABLE audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id INTEGER NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
INSERT INTO "audit_log" VALUES(1,2,'link','trainer_client',2,'{}','2026-09-07T21:11:41+00:00');
INSERT INTO "audit_log" VALUES(2,2,'create_draft','meal',1,'{"source": "photo", "after": {"id": 1, "client_user_id": 2, "source": "photo", "eaten_at": "2026-09-07T03:15:00+00:00", "local_date": "2026-09-07", "timezone": "Asia/Tokyo", "meal_type": "завтрак", "note": "Синтетическая запись миграции", "status": "draft", "created_at": "2026-09-07T21:11:41+00:00", "confirmed_at": null, "updated_at": "2026-09-07T21:11:41+00:00", "items": [{"id": 1, "meal_id": 1, "name": "Овсяная каша v1", "weight_g": 250.0, "portion_text": "", "calories": 310.0, "protein_g": 12.5, "fat_g": 7.25, "carbs_g": 48.0, "approximate": 1, "manually_edited": 0, "created_at": "2026-09-07T21:11:41+00:00"}], "comments": []}}','2026-09-07T21:11:41+00:00');
INSERT INTO "audit_log" VALUES(3,2,'confirm','meal',1,'{}','2026-09-07T21:11:41+00:00');
INSERT INTO "audit_log" VALUES(4,2,'add','water',1,'{"amount_ml": 450}','2026-09-07T21:11:41+00:00');
INSERT INTO "audit_log" VALUES(5,1,'set_norms','user',2,'{"before": null, "after": {"id": 1, "client_user_id": 2, "effective_from": "2026-09-07", "calories": 2100.0, "protein_g": 145.0, "fat_g": 70.0, "carbs_g": 240.0, "water_ml": 2300, "set_by_user_id": 1, "created_at": "2026-09-07T21:11:41+00:00"}}','2026-09-07T21:11:41+00:00');
INSERT INTO "audit_log" VALUES(6,1,'comment','meal',1,'{"comment_id": 1, "text": "Комментарий тренера v1"}','2026-09-07T21:11:41+00:00');
CREATE TABLE meal_items (
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
INSERT INTO "meal_items" VALUES(1,1,'Овсяная каша v1',250.0,'',310.0,12.5,7.25,48.0,1,0,'2026-09-07T21:11:41+00:00');
CREATE TABLE meals (
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
INSERT INTO "meals" VALUES(1,2,'photo','synthetic-file-id','2026-09-07T03:15:00+00:00','2026-09-07','Asia/Tokyo','завтрак','Синтетическая запись миграции','confirmed','2026-09-07T21:11:41+00:00','2026-09-07T21:11:41+00:00','v1-confirm-key','2026-09-07T21:11:41+00:00');
CREATE TABLE nutrition_norms (
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
INSERT INTO "nutrition_norms" VALUES(1,2,'2026-09-07',2100.0,145.0,70.0,240.0,2300,1,'2026-09-07T21:11:41+00:00');
CREATE TABLE photo_consents (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    version TEXT NOT NULL,
                    accepted_at TEXT NOT NULL
                );
INSERT INTO "photo_consents" VALUES(2,'nutrition-photo-v1','2026-09-07T21:11:41+00:00');
CREATE TABLE schema_meta (
                    version INTEGER NOT NULL
                );
INSERT INTO "schema_meta" VALUES(1);
CREATE TABLE trainer_clients (
                    trainer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    client_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    active INTEGER NOT NULL DEFAULT 1,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (trainer_user_id, client_user_id),
                    CHECK (trainer_user_id <> client_user_id)
                );
INSERT INTO "trainer_clients" VALUES(1,2,1,'2026-09-07T21:11:41+00:00');
CREATE TABLE trainer_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
                    trainer_user_id INTEGER NOT NULL REFERENCES users(id),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
INSERT INTO "trainer_comments" VALUES(1,1,1,'Комментарий тренера v1','2026-09-07T21:11:41+00:00');
CREATE TABLE trainer_invites (
                    code TEXT PRIMARY KEY,
                    trainer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    max_uses INTEGER NOT NULL DEFAULT 1,
                    uses INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
INSERT INTO "trainer_invites" VALUES('TWDISIVR',1,'2027-09-07T21:11:41+00:00',1,1,'2026-09-07T21:11:41+00:00');
CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
INSERT INTO "users" VALUES(1,71001,'Тренер v1','Europe/Moscow','2026-09-07T21:11:41+00:00','2026-09-07T21:11:41+00:00');
INSERT INTO "users" VALUES(2,72001,'Клиент v1','Asia/Tokyo','2026-09-07T21:11:41+00:00','2026-09-07T21:11:41+00:00');
CREATE TABLE water_logs (
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
INSERT INTO "water_logs" VALUES(1,2,450,'2026-09-07T04:00:00+00:00','2026-09-07','Asia/Tokyo','v1-water-key','2026-09-07T21:11:41+00:00');
CREATE INDEX idx_meals_client_date
                    ON meals(client_user_id, local_date, status);
CREATE INDEX idx_water_client_date
                    ON water_logs(client_user_id, local_date);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('users',2);
INSERT INTO "sqlite_sequence" VALUES('audit_log',6);
INSERT INTO "sqlite_sequence" VALUES('meals',1);
INSERT INTO "sqlite_sequence" VALUES('meal_items',1);
INSERT INTO "sqlite_sequence" VALUES('water_logs',1);
INSERT INTO "sqlite_sequence" VALUES('nutrition_norms',1);
INSERT INTO "sqlite_sequence" VALUES('trainer_comments',1);
COMMIT;
