
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

DB_PATH = Path(os.environ.get("OLIMPNAV_DB", str(Path(__file__).with_name("olimpnavigator.sqlite3"))))
INTERESTS = (
    "Математика", "Информатика", "Робототехника", "Физика", "Химия",
    "Биология", "История", "Обществознание", "Литература", "Языки",
    "Искусство", "Предпринимательство", "Другое",
)
EVENT_FIELDS = {
    "id", "title", "kind", "subjects", "grades", "min_age", "max_age",
    "regions", "organizer", "format", "registration_deadline", "stages",
    "result_date", "benefits", "eligibility", "description", "preparation",
    "official_url", "source_url", "checked_at",
}


@contextmanager
def connect(path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            yield conn


def init_db(path: Path = DB_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY, age INTEGER, grade INTEGER, region TEXT,
            school TEXT, interests TEXT NOT NULL DEFAULT '[]',
            notifications INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tracked (
            tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            favorite INTEGER NOT NULL DEFAULT 0,
            participating INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tg_id, event_id)
        );
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            event_id TEXT NOT NULL REFERENCES events(id),
            result TEXT NOT NULL,
            file_id TEXT,
            file_kind TEXT,
            file_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            points INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            reviewed_by INTEGER,
            UNIQUE(tg_id, event_id, result)
        );
        CREATE TABLE IF NOT EXISTS sent_notifications (
            tg_id INTEGER NOT NULL, event_id TEXT NOT NULL,
            notification_key TEXT NOT NULL, sent_at TEXT NOT NULL,
            PRIMARY KEY(tg_id, event_id, notification_key)
        );
        CREATE TABLE IF NOT EXISTS admin_deliveries (
            achievement_id INTEGER NOT NULL REFERENCES achievements(id) ON DELETE CASCADE,
            admin_id INTEGER NOT NULL, sent_at TEXT NOT NULL,
            PRIMARY KEY(achievement_id, admin_id)
        );
        CREATE TABLE IF NOT EXISTS ai_usage (
            tg_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            day TEXT NOT NULL,
            messages INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(tg_id, day)
        );
        CREATE TABLE IF NOT EXISTS ai_sessions (
            tg_id INTEGER PRIMARY KEY REFERENCES users(tg_id) ON DELETE CASCADE,
            event_id TEXT,
            history TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS partner_messages (
            sender_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            recipient_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(sender_id, recipient_id, day)
        );
        """)
        columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
        if 'display_name' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN display_name TEXT')
        if 'partner_public' not in columns:
            db.execute('ALTER TABLE users ADD COLUMN partner_public INTEGER NOT NULL DEFAULT 0')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(achievements)')}
        if 'review_comment' not in columns:
            db.execute('ALTER TABLE achievements ADD COLUMN review_comment TEXT')
        if 'review_notified_at' not in columns:
            db.execute('ALTER TABLE achievements ADD COLUMN review_notified_at TEXT')
            db.execute("""UPDATE achievements SET review_notified_at=created_at
                WHERE status IN ('approved','rejected')""")


def ensure_user(tg_id: int, path: Path = DB_PATH) -> None:
   with connect(path) as db:
       db.execute("INSERT OR IGNORE INTO users(tg_id, created_at) VALUES (?, ?)",
                  (tg_id, datetime.now().isoformat(timespec="seconds")))


def remember_name(tg_id: int, profile: dict, path: Path = DB_PATH) -> None:
    name = ' '.join(str(profile.get(key) or '').strip() for key in ('first_name', 'last_name')).strip()
    if not name:
        name = str(profile.get('username') or profile.get('name') or '').strip()
    if name:
        with connect(path) as db:
            db.execute('UPDATE users SET display_name=? WHERE tg_id=?', (name[:120], tg_id))


def get_user(tg_id: int, path: Path = DB_PATH) -> dict | None:
     with connect(path) as db:
         row = db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
     if not row:
         return None
     user = dict(row)
     user["interests"] = json.loads(user["interests"])
     return user


def update_user(tg_id: int, path: Path = DB_PATH, **fields) -> None:
    allowed = {"age", "grade", "region", "school", "interests", "notifications", "partner_public"}
    if not fields or set(fields) - allowed:
        raise ValueError("Недопустимые поля профиля")
    ensure_user(tg_id, path)
    if "interests" in fields:
        fields["interests"] = json.dumps(fields["interests"], ensure_ascii=False)
    sql = "UPDATE users SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE tg_id=?"
    with connect(path) as db:
        db.execute(sql, (*fields.values(), tg_id))


def partner_users(tg_id: int, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as db:
        rows = db.execute("""SELECT tg_id,display_name,grade,school,interests FROM users
                            WHERE partner_public=1 AND tg_id<>? AND grade IS NOT NULL
                            ORDER BY display_name COLLATE NOCASE, tg_id""", (tg_id,)).fetchall()
    users = []
    for row in rows:
        user = dict(row)
        user['interests'] = json.loads(user['interests'])
        users.append(user)
    return users


def reserve_partner_message(sender_id: int, recipient_id: int, day: str,
                            path: Path = DB_PATH) -> str:
    if sender_id == recipient_id:
        return 'unavailable'
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('SELECT tg_id,partner_public FROM users WHERE tg_id IN (?,?)',
                          (sender_id, recipient_id)).fetchall()
        if len(rows) != 2 or not all(row['partner_public'] for row in rows):
            return 'unavailable'
        total = db.execute('SELECT COALESCE(SUM(count),0) AS total FROM partner_messages WHERE sender_id=? AND day=?',
                           (sender_id, day)).fetchone()['total']
        if total >= 20:
            return 'daily_limit'
        count = db.execute('SELECT count FROM partner_messages WHERE sender_id=? AND recipient_id=? AND day=?',
                           (sender_id, recipient_id, day)).fetchone()
        if count and count['count'] >= 5:
            return 'limit'
        db.execute('INSERT INTO partner_messages(sender_id,recipient_id,day,count) VALUES(?,?,?,1) '
                   'ON CONFLICT(sender_id,recipient_id,day) DO UPDATE SET count=count+1',
                   (sender_id, recipient_id, day))
    return 'ok'


def ai_messages_today(tg_id: int, day: str, path: Path = DB_PATH) -> int:
    with connect(path) as db:
        row = db.execute("SELECT messages FROM ai_usage WHERE tg_id=? AND day=?",
                         (tg_id, day)).fetchone()
    return row["messages"] if row else 0


def use_ai_message(tg_id: int, day: str, limit: int = 50, path: Path = DB_PATH) -> bool:
    ensure_user(tg_id, path)
    with connect(path) as db:
        row = db.execute("SELECT messages FROM ai_usage WHERE tg_id=? AND day=?",
                         (tg_id, day)).fetchone()
        if row and row["messages"] >= limit:
            return False
        db.execute("INSERT INTO ai_usage(tg_id, day, messages) VALUES (?, ?, 1) "
                   "ON CONFLICT(tg_id, day) DO UPDATE SET messages=messages+1",
                   (tg_id, day))
    return True


def get_ai_session(tg_id: int, path: Path = DB_PATH) -> dict | None:
    with connect(path) as db:
        row = db.execute("SELECT event_id, history FROM ai_sessions WHERE tg_id=?",
                         (tg_id,)).fetchone()
    return {"event_id": row["event_id"], "history": json.loads(row["history"])} if row else None


def save_ai_session(tg_id: int, event_id=None, history=None, path: Path = DB_PATH) -> None:
    ensure_user(tg_id, path)
    with connect(path) as db:
        db.execute("INSERT INTO ai_sessions(tg_id, event_id, history) VALUES (?, ?, ?) "
                   "ON CONFLICT(tg_id) DO UPDATE SET event_id=excluded.event_id, history=excluded.history",
                   (tg_id, event_id, json.dumps(history or [], ensure_ascii=False)))


def end_ai_session(tg_id: int, path: Path = DB_PATH) -> None:
    with connect(path) as db:
        db.execute("DELETE FROM ai_sessions WHERE tg_id=?", (tg_id,))


def _date(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field}: нужна дата YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def _url(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}: нужна ссылка http(s)")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or any(char.isspace() for char in value):
        raise ValueError(f"{field}: нужна ссылка http(s)")
    return value


def validate_event(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Мероприятие должно быть объектом")
    missing = EVENT_FIELDS - set(raw)
    if missing:
        raise ValueError("Нет полей: " + ", ".join(sorted(missing)))
    event = {k: raw[k] for k in EVENT_FIELDS}
    event["rsosh_level"] = raw.get("rsosh_level")
    if event["rsosh_level"] is not None and (type(event["rsosh_level"]) is not int or event["rsosh_level"] not in (0, 1, 2, 3)):
        raise ValueError("rsosh_level: нужен 0, 1, 2, 3 или null")
    event["image_url"] = raw.get("image_url")
    if event["image_url"] is not None:
        event["image_url"] = _url(event["image_url"], "image_url")
    for field in ("id", "title", "kind", "organizer", "format", "benefits",
                  "eligibility", "description"):
        if not isinstance(event[field], str) or not event[field].strip():
            raise ValueError(f"{field}: непустой текст обязателен")
        event[field] = event[field].strip()
    if len(event["id"]) > 48 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in event["id"]):
        raise ValueError("id: только латинские буквы, цифры, - и _")
    for field in ("subjects", "regions"):
        if not isinstance(event[field], list) or not all(isinstance(x, str) and x.strip() for x in event[field]):
            raise ValueError(f"{field}: список строк")
    if not isinstance(event["grades"], list) or not all(type(x) is int and 1 <= x <= 11 for x in event["grades"]):
        raise ValueError("grades: список классов 1-11")
    for field in ("min_age", "max_age"):
        if event[field] is not None and (type(event[field]) is not int or not 6 <= event[field] <= 25):
            raise ValueError(f"{field}: число 6-25 или null")
    if event["min_age"] and event["max_age"] and event["min_age"] > event["max_age"]:
        raise ValueError("min_age больше max_age")
    event["official_url"] = _url(event["official_url"], "official_url")
    event["source_url"] = _url(event["source_url"], "source_url")
    for field in ("registration_deadline", "result_date", "checked_at"):
        event[field] = _date(event[field], field)
    if not event["checked_at"]:
        raise ValueError("checked_at обязателен")
    if not isinstance(event["stages"], list):
        raise ValueError("stages: список")
    for stage in event["stages"]:
        if not isinstance(stage, dict) or not isinstance(stage.get("name"), str) or not stage["name"].strip():
            raise ValueError("stages: у каждого этапа нужно имя")
        stage["date"] = _date(stage.get("date"), "stages.date")
    if not isinstance(event["preparation"], list):
        raise ValueError("preparation: список")
    for material in event["preparation"]:
        if not isinstance(material, dict) or not isinstance(material.get("title"), str) or not material["title"].strip():
            raise ValueError("preparation: нужно название")
        material["url"] = _url(material.get("url"), "preparation.url")
    return event


def import_events(items: list, path: Path = DB_PATH) -> tuple[int, int, list[tuple[str, str]]]:
    if not isinstance(items, list):
        raise ValueError("Корень файла должен быть массивом")
    checked = [(validate_event(item), "rsosh_level" in item) for item in items]
    if len({item["id"] for item, _ in checked}) != len(checked):
        raise ValueError("Повторяющиеся id")
    added = updated = 0
    changes = []
    with connect(path) as db:
        for item, level_supplied in checked:
            old_row = db.execute("SELECT payload FROM events WHERE id=?", (item["id"],)).fetchone()
            old = json.loads(old_row["payload"]) if old_row else None
            if old and not level_supplied:
                item["rsosh_level"] = old.get("rsosh_level")
            if old == item:
                continue
            if old:
                updated += 1
                for key, label in (("registration_deadline", "срок регистрации"),
                                   ("stages", "этапы"), ("result_date", "дата результатов"),
                                   ("rsosh_level", "уровень РСОШ")):
                    if old.get(key) != item.get(key):
                        changes.append((item["id"], label))
            else:
                added += 1
            db.execute("INSERT INTO events(id,payload,updated_at) VALUES(?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                       (item["id"], json.dumps(item, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
            if old and old.get("rsosh_level") != item.get("rsosh_level"):
                rows = db.execute("SELECT id,result FROM achievements WHERE event_id=? AND status='approved'",
                                  (item["id"],)).fetchall()
                for achievement in rows:
                    db.execute("UPDATE achievements SET points=? WHERE id=?",
                               (achievement_points(achievement["result"], item["rsosh_level"]), achievement["id"]))
    return added, updated, changes


def get_event(event_id: str, path: Path = DB_PATH) -> dict | None:
    with connect(path) as db:
        row = db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def create_custom_event(tg_id: int, title: str, kind: str, path: Path = DB_PATH) -> str:
    if kind not in ('олимпиада', 'конкурс'):
        raise ValueError('Неверный тип мероприятия')
    title = ' '.join(title.split())[:150]
    if not title:
        raise ValueError('Название не указано')
    digest = hashlib.sha256(f'{tg_id}:{kind}:{title.casefold()}'.encode()).hexdigest()[:24]
    event_id = f'custom_{tg_id}_{digest}'
    event = {'id': event_id, 'title': title, 'kind': kind, 'rsosh_level': None,
             'custom': True, 'owner_id': tg_id}
    with connect(path) as db:
        db.execute('INSERT OR IGNORE INTO events(id,payload,updated_at) VALUES(?,?,?)',
                   (event_id, json.dumps(event, ensure_ascii=False), datetime.now().isoformat(timespec='seconds')))
    return event_id


def all_events(path: Path = DB_PATH) -> list[dict]:
    with connect(path) as db:
        rows = db.execute("SELECT payload FROM events").fetchall()
    events = []
    for row in rows:
        event = json.loads(row['payload'])
        if not event.get('custom'):
            events.append(event)
    return events


def matches(event: dict, user: dict) -> bool:
    if user.get("grade") and event["grades"] and user["grade"] not in event["grades"]:
        return False
    if user.get("age") and event["min_age"] and user["age"] < event["min_age"]:
        return False
    if user.get("age") and event["max_age"] and user["age"] > event["max_age"]:
        return False
    regions = [x.casefold() for x in event["regions"]]
    if regions and "вся россия" not in regions and user.get("region") and user["region"].casefold() not in regions:
        return False
    interests = {x.casefold() for x in user.get("interests", [])}
    subjects = {x.casefold() for x in event["subjects"]}
    if interests and "другое" not in interests and subjects and not interests & subjects:
        return False
    return True


def recommended(user: dict, today: date | None = None, path: Path = DB_PATH) -> list[dict]:
    today = today or date.today()
    events = []
    for event in all_events(path):
        if not matches(event, user):
            continue
        if date.fromisoformat(event["checked_at"]) < today - timedelta(days=30):
            continue
        known = []
        for day in (event["registration_deadline"], event["result_date"]):
            if day:
                known.append(day)
        for stage in event["stages"]:
            if stage["date"]:
                known.append(stage["date"])
        if known and max(known) < today.isoformat():
            continue
        events.append(event)
    interests = set()
    for name in user.get("interests", []):
        interests.add(name.casefold())

    def sort_key(event):
        subjects = set()
        for name in event["subjects"]:
            subjects.add(name.casefold())
        has_interest = bool(interests & subjects)
        deadline = event["registration_deadline"] or "9999-12-31"
        return (not has_interest, event["registration_deadline"] is None, deadline, event["title"])

    events.sort(key=sort_key)
    return events


def track(tg_id: int, event_id: str, field: str, value: bool, path: Path = DB_PATH) -> None:
    if field not in ("favorite", "participating"):
        raise ValueError("Неизвестное действие")
    with connect(path) as db:
        db.execute("INSERT OR IGNORE INTO tracked(tg_id,event_id) VALUES(?,?)", (tg_id, event_id))
        db.execute(f"UPDATE tracked SET {field}=? WHERE tg_id=? AND event_id=?", (int(value), tg_id, event_id))


def tracked_ids(tg_id: int, field: str, path: Path = DB_PATH) -> list[str]:
    if field not in ("favorite", "participating"):
        raise ValueError("Неизвестное поле")
    with connect(path) as db:
        rows = db.execute(f"SELECT event_id FROM tracked WHERE tg_id=? AND {field}=1", (tg_id,)).fetchall()
    return [row["event_id"] for row in rows]


def is_tracked(tg_id: int, event_id: str, path: Path = DB_PATH) -> dict:
    with connect(path) as db:
        row = db.execute("SELECT favorite,participating FROM tracked WHERE tg_id=? AND event_id=?",
                         (tg_id, event_id)).fetchone()
    return dict(row) if row else {"favorite": 0, "participating": 0}


def due_notifications(today: date, path: Path = DB_PATH) -> list[tuple[int, dict, str, str]]:
    with connect(path) as db:
        rows = db.execute("""SELECT t.tg_id, e.payload FROM tracked t
            JOIN users u ON u.tg_id=t.tg_id JOIN events e ON e.id=t.event_id
            WHERE t.participating=1 AND u.notifications=1""").fetchall()
        sent = {(r[0], r[1], r[2]) for r in db.execute(
            "SELECT tg_id,event_id,notification_key FROM sent_notifications").fetchall()}
    result = []
    for row in rows:
        event = json.loads(row["payload"])
        dates = []
        if event["registration_deadline"]:
            deadline = date.fromisoformat(event["registration_deadline"])
            dates.extend([(deadline - timedelta(days=3), f"reg-3-{deadline.isoformat()}", "До конца регистрации 3 дня"),
                          (deadline - timedelta(days=1), f"reg-1-{deadline.isoformat()}", "Регистрация заканчивается завтра")])
        for index, stage in enumerate(event["stages"]):
            if stage["date"]:
                dates.append((date.fromisoformat(stage["date"]), f"stage-{index}-{stage['date']}",
                              f"Сегодня: {stage['name']}"))
        if event["result_date"]:
            dates.append((date.fromisoformat(event["result_date"]), f"results-{event['result_date']}", "Сегодня ожидаются результаты"))
        for when, key, message in dates:
            if when == today and (row["tg_id"], event["id"], key) not in sent:
                result.append((row["tg_id"], event, key, message))
    return result


def mark_sent(tg_id: int, event_id: str, key: str, path: Path = DB_PATH) -> None:
    with connect(path) as db:
        db.execute("INSERT OR IGNORE INTO sent_notifications VALUES(?,?,?,?)",
                   (tg_id, event_id, key, datetime.now().isoformat(timespec="seconds")))


POINTS = {
    1: {"участник": 10, "призёр": 60, "победитель": 100},
    2: {"участник": 7, "призёр": 42, "победитель": 70},
    3: {"участник": 5, "призёр": 30, "победитель": 50},
    0: {"участник": 2, "призёр": 12, "победитель": 20},
}


def achievement_points(result: str, rsosh_level: int | None) -> int:
    if result not in POINTS[0]:
        raise ValueError("Неизвестный результат")
    return POINTS[rsosh_level if rsosh_level in (1, 2, 3) else 0][result]


def add_achievement(tg_id: int, event_id: str, result: str, file_id: str | None,
                    file_kind: str | None, file_name: str | None, path: Path = DB_PATH) -> int:
    if result not in POINTS[0]:
        raise ValueError("Неизвестный результат")
    with connect(path) as db:
        pending = db.execute("SELECT id FROM achievements WHERE tg_id=? AND event_id=? AND status='pending'",
                             (tg_id, event_id)).fetchone()
        if pending:
            raise sqlite3.IntegrityError("Результат этого события уже проверяется")
        existing = db.execute("SELECT id,status FROM achievements WHERE tg_id=? AND event_id=? AND result=?",
                              (tg_id, event_id, result)).fetchone()
        if existing:
            if existing["status"] not in ("rejected", "superseded"):
                raise sqlite3.IntegrityError("Результат уже добавлен")
            db.execute("""UPDATE achievements SET file_id=?,file_kind=?,file_name=?,status='pending',
                points=0,created_at=?,reviewed_by=NULL,review_comment=NULL,review_notified_at=NULL WHERE id=?""",
                (file_id, file_kind, file_name, datetime.now().isoformat(timespec="seconds"), existing["id"]))
            db.execute("DELETE FROM admin_deliveries WHERE achievement_id=?", (existing["id"],))
            return existing["id"]
        cursor = db.execute("""INSERT INTO achievements
            (tg_id,event_id,result,file_id,file_kind,file_name,created_at)
            VALUES(?,?,?,?,?,?,?)""", (tg_id, event_id, result, file_id, file_kind, file_name,
                                      datetime.now().isoformat(timespec="seconds")))
        return cursor.lastrowid


def pending_achievement(achievement_id: int, path: Path = DB_PATH) -> dict | None:
    with connect(path) as db:
        row = db.execute("SELECT * FROM achievements WHERE id=? AND status='pending'", (achievement_id,)).fetchone()
    return dict(row) if row else None


def review_achievement(achievement_id: int, admin_id: int, approved: bool,
                       comment: str = '', path: Path = DB_PATH) -> dict | None:
    comment = ' '.join(comment.split())[:1000]
    if not approved and not comment:
        raise ValueError('Укажи причину отклонения')
    with connect(path) as db:
        row = db.execute("SELECT * FROM achievements WHERE id=? AND status='pending'", (achievement_id,)).fetchone()
        if not row:
            return None
        event_row = db.execute("SELECT payload FROM events WHERE id=?", (row["event_id"],)).fetchone()
        level = json.loads(event_row["payload"]).get("rsosh_level") if event_row else None
        if approved:
            db.execute("""UPDATE achievements SET status='superseded',points=0
                WHERE tg_id=? AND event_id=? AND status='approved' AND id<>?""",
                (row["tg_id"], row["event_id"], achievement_id))
        db.execute("""UPDATE achievements SET status=?,points=?,reviewed_by=?,
            review_comment=?,review_notified_at=NULL WHERE id=?""",
                   ("approved" if approved else "rejected", achievement_points(row["result"], level) if approved else 0,
                    admin_id, comment or None, achievement_id))
    result = dict(row)
    result['points'] = achievement_points(row['result'], level) if approved else 0
    result['review_comment'] = comment or None
    result['status'] = 'approved' if approved else 'rejected'
    return result


def pending_review_notifications(path: Path = DB_PATH) -> list[dict]:
    with connect(path) as db:
        rows = db.execute("""SELECT * FROM achievements WHERE status IN ('approved','rejected')
            AND review_notified_at IS NULL ORDER BY id LIMIT 100""").fetchall()
    return [dict(row) for row in rows]


def pending_review_notice(achievement_id: int, path: Path = DB_PATH) -> dict | None:
    with connect(path) as db:
        row = db.execute("""SELECT * FROM achievements WHERE id=?
            AND status IN ('approved','rejected') AND review_notified_at IS NULL""",
                         (achievement_id,)).fetchone()
    return dict(row) if row else None


def mark_review_notified(achievement_id: int, path: Path = DB_PATH) -> None:
    with connect(path) as db:
        db.execute("UPDATE achievements SET review_notified_at=? WHERE id=?",
                   (datetime.now().isoformat(timespec='seconds'), achievement_id))


def achievements_for(tg_id: int, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as db:
        rows = db.execute("SELECT * FROM achievements WHERE tg_id=? ORDER BY id DESC", (tg_id,)).fetchall()
    return [dict(r) for r in rows]


def pending_reviews_for(admin_id: int, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as db:
        rows = db.execute("""SELECT a.* FROM achievements a
            WHERE a.status='pending' AND NOT EXISTS (
                SELECT 1 FROM admin_deliveries d
                WHERE d.achievement_id=a.id AND d.admin_id=?
            ) ORDER BY a.id""", (admin_id,)).fetchall()
    return [dict(row) for row in rows]


def mark_admin_delivered(achievement_id: int, admin_id: int, path: Path = DB_PATH) -> None:
    with connect(path) as db:
        db.execute("INSERT OR IGNORE INTO admin_deliveries VALUES(?,?,?)",
                   (achievement_id, admin_id, datetime.now().isoformat(timespec="seconds")))


def leaderboard(school: str | None = None, path: Path = DB_PATH) -> list[dict]:
    with connect(path) as db:
        if school:
            rows = db.execute("""SELECT u.tg_id,u.school,u.display_name,SUM(a.points) AS points
                FROM users u JOIN (
                    SELECT tg_id,event_id,MAX(points) AS points FROM achievements
                    WHERE status='approved' GROUP BY tg_id,event_id
                ) a ON a.tg_id=u.tg_id
                WHERE u.school=? GROUP BY u.tg_id ORDER BY points DESC LIMIT 10""", (school,)).fetchall()
        else:
            rows = db.execute("""SELECT u.tg_id,u.school,u.display_name,SUM(a.points) AS points
                FROM users u JOIN (
                    SELECT tg_id,event_id,MAX(points) AS points FROM achievements
                    WHERE status='approved' GROUP BY tg_id,event_id
                ) a ON a.tg_id=u.tg_id
                GROUP BY u.tg_id ORDER BY points DESC LIMIT 10""").fetchall()
    return [dict(r) for r in rows]


def subscribers_for(event_id: str, path: Path = DB_PATH) -> list[int]:
    with connect(path) as db:
        rows = db.execute("""SELECT t.tg_id FROM tracked t JOIN users u ON u.tg_id=t.tg_id
            WHERE t.event_id=? AND t.participating=1 AND u.notifications=1""", (event_id,)).fetchall()
    return [r[0] for r in rows]


REGIONS = (
    "Москва", "Московская область", "Санкт-Петербург", "Ленинградская область",
    "Свердловская область", "Тюменская область", "Челябинская область", "Новосибирская область",
    "Республика Адыгея", "Республика Алтай", "Республика Башкортостан", "Республика Бурятия",
    "Республика Дагестан", "Республика Ингушетия", "Кабардино-Балкарская Республика",
    "Республика Калмыкия", "Карачаево-Черкесская Республика", "Республика Карелия",
    "Республика Коми", "Республика Крым", "Республика Марий Эл", "Республика Мордовия",
    "Республика Саха (Якутия)", "Республика Северная Осетия - Алания", "Республика Татарстан",
    "Республика Тыва", "Удмуртская Республика", "Республика Хакасия", "Чеченская Республика",
    "Чувашская Республика", "Алтайский край", "Забайкальский край", "Камчатский край",
    "Краснодарский край", "Красноярский край", "Пермский край", "Приморский край",
    "Ставропольский край", "Хабаровский край", "Амурская область", "Архангельская область",
    "Астраханская область", "Белгородская область", "Брянская область", "Владимирская область",
    "Волгоградская область", "Вологодская область", "Воронежская область", "Ивановская область",
    "Иркутская область", "Калининградская область", "Калужская область", "Кемеровская область",
    "Кировская область", "Костромская область", "Курганская область", "Курская область",
    "Липецкая область", "Магаданская область", "Мурманская область", "Нижегородская область",
    "Новгородская область", "Омская область", "Оренбургская область", "Орловская область",
    "Пензенская область", "Псковская область", "Ростовская область", "Рязанская область",
    "Самарская область", "Саратовская область", "Сахалинская область", "Смоленская область",
    "Тамбовская область", "Тверская область", "Томская область", "Тульская область",
    "Ульяновская область", "Ярославская область", "Еврейская автономная область",
    "Ненецкий автономный округ", "Ханты-Мансийский автономный округ - Югра",
    "Чукотский автономный округ", "Ямало-Ненецкий автономный округ",
)
