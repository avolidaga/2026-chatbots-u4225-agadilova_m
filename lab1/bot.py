"""Личный Telegram-ассистент: задачи и сохраняемые в SQLite напоминания."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from errors import InputError
from task_dates import deadline_day

BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
CHECK_INTERVAL = 10  # Напоминания проверяются каждые 10 секунд.
PRIORITIES = {"высокий": 0, "средний": 1, "низкий": 2}
PRIORITY_LABELS = ("🔴 высокий", "🟡 средний", "🟢 низкий")
HELP = """Отправьте текст, чтобы добавить задачу, или используйте команды:
/add — пошагово добавить задачу с описанием
/add Купить продукты — быстро добавить задачу без описания
/list — невыполненные задачи
/list_all — все задачи
/search <текст> — поиск по части названия или описания
/edit <id> — изменить название и описание
/group_create — создать группу
/groups — мои группы и количество активных задач
/group_tasks <group_id> — все задачи группы
/group_rename <group_id> — переименовать группу
/group_delete <group_id> — удалить группу, сохранив задачи
/task_group <id> — выбрать или изменить группу задачи
/task_ungroup <id> — убрать задачу из группы
/priority <id> высокий — изменить приоритет (высокий, средний, низкий)
/done <id> — выполнить задачу и отменить напоминание
/delete <id> — удалить задачу
/remind <id> 15.10 18:30 — установить или заменить напоминание
/remind_off <id> — отменить напоминание
/cancel — отменить текущий диалог
/help — помощь

В примерах замените <id> на ID своей задачи, а дату — на будущую.
Формат даты: ДД.ММ ЧЧ:ММ (текущий год), также доступен ДД.ММ.ГГГГ ЧЧ:ММ.
"""


@dataclass
class TaskDraft:
    """Незавершённая задача; отдельный черновик для каждого личного чата."""

    step: str = "title"
    title: str = ""
    description: str = ""
    priority: int = 1
    mode: str = "add"
    target_id: int | None = None
    group_id: int | None = None


def validate_task_text(title: str, description: str = "") -> None:
    if not title.strip():
        raise InputError("Название не должно быть пустым. Введите название задачи.")
    # Общий лимит оставляет место для служебных строк в сообщениях Telegram.
    if len((title + description).encode("utf-16-le")) // 2 > 3000:
        raise InputError("Сократите название и описание: вместе не более 3000 символов (эмодзи могут занимать два).")


def validate_group_name(name: str) -> str:
    name = name.strip()
    if not name or "\n" in name or "\r" in name or len(name.encode("utf-16-le")) // 2 > 80:
        raise InputError("Введите название группы: одна непустая строка, не более 80 символов.")
    return name


@dataclass(frozen=True)
class Settings:
    token: str
    timezone: ZoneInfo
    database: Path

    @classmethod
    def load(cls) -> Settings:
        # Читаем именно lab1/.env, независимо от каталога запуска.
        load_dotenv(BASE_DIR / ".env")
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise InputError(
                "Не задан TELEGRAM_BOT_TOKEN. Скопируйте .env.example в .env "
                "в папке lab1 и добавьте токен от @BotFather."
            )
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise InputError("Неверный формат TELEGRAM_BOT_TOKEN. Проверьте токен от @BotFather.")
        try:
            tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow").strip())
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise InputError("Неизвестный TIMEZONE. Пример: Europe/Moscow.") from exc
        path = os.getenv("DATABASE_PATH", "data/tasks.sqlite3").strip()
        if not path:
            raise InputError("DATABASE_PATH не должен быть пустым. Пример: data/tasks.sqlite3.")
        database = Path(path).expanduser()
        if not database.is_absolute():
            database = BASE_DIR / database
        return cls(token, tz, database)


class SafeFormatter(logging.Formatter):
    """Убирает токены даже из адресов запросов и текстов исключений."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if token:
            text = text.replace(token, "<TOKEN>")
        return re.sub(r"(?<![0-9])\d+:[A-Za-z0-9_-]{10,}", "<TOKEN>", text)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # HTTP-библиотека иначе выводит URL каждого запроса к боту.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


class TaskStore:
    """Небольшое хранилище. Каждый пользовательский запрос проверяет владельца."""

    TASK_SELECT = (
        "SELECT t.*, g.name AS group_name FROM tasks AS t "
        "LEFT JOIN task_groups AS g ON g.id = t.group_id AND g.user_id = t.user_id "
    )

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        # SQLite LOWER/LIKE не обеспечивают полный поиск по кириллице. Casefold
        # применяется в SQL к обоим полям, а запрос передаётся как обычный параметр.
        self.connection.create_function("CASEFOLD", 1, str.casefold, deterministic=True)
        try:
            with self.connection:
                self.connection.executescript("""
                    CREATE TABLE IF NOT EXISTS task_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        name_key TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        UNIQUE(user_id, name_key)
                    );
                    CREATE TABLE IF NOT EXISTS tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        text TEXT NOT NULL CHECK(length(trim(text)) > 0),
                        description TEXT NOT NULL DEFAULT '',
                        priority INTEGER NOT NULL DEFAULT 1 CHECK(priority BETWEEN 0 AND 2),
                        done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0, 1)),
                        created_at REAL NOT NULL,
                        remind_at REAL,
                        reminder_state TEXT NOT NULL DEFAULT 'none'
                            CHECK(reminder_state IN ('none', 'pending', 'sent', 'failed')),
                        next_attempt_at REAL,
                        sent_at REAL,
                        group_id INTEGER REFERENCES task_groups(id) ON DELETE SET NULL
                    );
                    CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(user_id);
                    CREATE INDEX IF NOT EXISTS tasks_reminders
                        ON tasks(reminder_state, next_attempt_at);
                """)
                # Миграция добавляет поля без пересоздания старой таблицы.
                columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)")}
                if "description" not in columns:
                    self.connection.execute("ALTER TABLE tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''")
                if "group_id" not in columns:
                    self.connection.execute(
                        "ALTER TABLE tasks ADD COLUMN group_id INTEGER REFERENCES task_groups(id) ON DELETE SET NULL"
                    )
                # Новая миграция не меняет прежние сроки доставки или статусы.
                for name, definition in (
                    ("deadline_at", "REAL"),
                    ("deadline_has_time", "INTEGER NOT NULL DEFAULT 1 CHECK(deadline_has_time IN (0, 1))"),
                    ("revision", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if name not in columns:
                        self.connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
                group_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(task_groups)")}
                if "revision" not in group_columns:
                    self.connection.execute("ALTER TABLE task_groups ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
                self.connection.execute("CREATE INDEX IF NOT EXISTS tasks_group ON tasks(user_id, group_id)")
        except sqlite3.Error:
            self.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def get(self, task_id: int, user_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            self.TASK_SELECT + "WHERE t.id = ? AND t.user_id = ?", (task_id, user_id)
        ).fetchone()
        if row is None:
            raise InputError("Задача не найдена. Посмотрите ID своих задач: /list_all.")
        return row

    def add(
        self, user_id: int, chat_id: int, text: str, description: str = "",
        priority: int = 1, remind_at: float | None = None,
        group_id: int | None = None,
        deadline_at: float | None = None, deadline_has_time: bool = True,
    ) -> sqlite3.Row:
        text = text.strip()
        description = description.strip()
        validate_task_text(text, description)
        if group_id is not None:
            self.get_group(group_id, user_id)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO tasks(user_id, chat_id, text, description, priority, created_at, "
                "remind_at, reminder_state, next_attempt_at, group_id, deadline_at, deadline_has_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, chat_id, text, description, priority, datetime.now(UTC).timestamp(),
                 remind_at, "pending" if remind_at is not None else "none", remind_at, group_id,
                 deadline_at, int(deadline_has_time)),
            )
            if group_id is not None:
                self.connection.execute("UPDATE task_groups SET revision = revision + 1 WHERE id = ?", (group_id,))
            # Читаем результат до commit: сбой чтения тоже откатывает вставку.
            row = self.get(cursor.lastrowid, user_id)
        return row

    def list_tasks(
        self, user_id: int, include_done: bool = False, group_id: int | None = None,
    ) -> list[sqlite3.Row]:
        if group_id is not None:
            self.get_group(group_id, user_id)
        return self.connection.execute(
            self.TASK_SELECT + "WHERE t.user_id = ? AND (? OR t.done = 0) "
            "AND (? IS NULL OR t.group_id = ?) ORDER BY t.priority, t.created_at, t.id",
            (user_id, include_done, group_id, group_id),
        ).fetchall()

    def search(self, user_id: int, query: str) -> list[sqlite3.Row]:
        query = query.strip().casefold()
        if not query:
            raise InputError("Введите часть названия или описания: /search продук.")
        return self.connection.execute(
            self.TASK_SELECT + "WHERE t.user_id = ? "
            "AND (instr(CASEFOLD(t.text), ?) > 0 OR instr(CASEFOLD(t.description), ?) > 0) "
            "ORDER BY t.priority, t.created_at, t.id", (user_id, query, query),
        ).fetchall()

    def update_text(self, task_id: int, user_id: int, title: str, description: str) -> None:
        self.get(task_id, user_id)
        title, description = title.strip(), description.strip()
        validate_task_text(title, description)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET text = ?, description = ?, revision = revision + 1 WHERE id = ? AND user_id = ?",
                (title, description, task_id, user_id),
            )

    def get_group(self, group_id: int, user_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM task_groups WHERE id = ? AND user_id = ?", (group_id, user_id),
        ).fetchone()
        if row is None:
            raise InputError("Группа не найдена. ID своих групп: /groups.")
        return row

    def list_groups(self, user_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT g.*, COUNT(t.id) AS total_count, "
            "COUNT(CASE WHEN t.done = 0 THEN 1 END) AS active_count "
            "FROM task_groups AS g LEFT JOIN tasks AS t "
            "ON t.group_id = g.id AND t.user_id = g.user_id "
            "WHERE g.user_id = ? GROUP BY g.id ORDER BY g.created_at, g.id", (user_id,),
        ).fetchall()

    def create_group(self, user_id: int, name: str) -> sqlite3.Row:
        name = validate_group_name(name)
        try:
            with self.connection:
                cursor = self.connection.execute(
                    "INSERT INTO task_groups(user_id, name, name_key, created_at) VALUES (?, ?, ?, ?)",
                    (user_id, name, name.casefold(), datetime.now(UTC).timestamp()),
                )
                row = self.get_group(cursor.lastrowid, user_id)
        except sqlite3.IntegrityError as exc:
            raise InputError("Группа с таким названием уже существует. Выберите другое название.") from exc
        return row

    def rename_group(self, group_id: int, user_id: int, name: str) -> None:
        self.get_group(group_id, user_id)
        name = validate_group_name(name)
        try:
            with self.connection:
                self.connection.execute(
                    "UPDATE task_groups SET name = ?, name_key = ?, revision = revision + 1 WHERE id = ? AND user_id = ?",
                    (name, name.casefold(), group_id, user_id),
                )
        except sqlite3.IntegrityError as exc:
            raise InputError("Группа с таким названием уже существует. Выберите другое название.") from exc

    def delete_group(self, group_id: int, user_id: int) -> None:
        self.get_group(group_id, user_id)
        # Одна транзакция: задачи и их напоминания остаются, удаляется лишь связь.
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET group_id = NULL, revision = revision + 1 WHERE group_id = ? AND user_id = ?", (group_id, user_id),
            )
            self.connection.execute("DELETE FROM task_groups WHERE id = ? AND user_id = ?", (group_id, user_id))

    def assign_group(self, task_id: int, user_id: int, group_id: int | None) -> None:
        row = self.get(task_id, user_id)
        if group_id is not None:
            self.get_group(group_id, user_id)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET group_id = ?, revision = revision + 1 WHERE id = ? AND user_id = ?", (group_id, task_id, user_id),
            )
            self.connection.execute(
                "UPDATE task_groups SET revision = revision + 1 WHERE user_id = ? AND (id = ? OR id = ?)",
                (user_id, row["group_id"], group_id),
            )

    def set_priority(self, task_id: int, user_id: int, priority: int) -> None:
        self.get(task_id, user_id)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET priority = ?, revision = revision + 1 WHERE id = ? AND user_id = ?",
                (priority, task_id, user_id),
            )

    def finish(self, task_id: int, user_id: int) -> None:
        row = self.get(task_id, user_id)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET done = 1, remind_at = NULL, reminder_state = 'none', "
                "next_attempt_at = NULL, sent_at = NULL, revision = revision + 1 WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
            if row["group_id"] is not None:
                self.connection.execute("UPDATE task_groups SET revision = revision + 1 WHERE id = ?", (row["group_id"],))

    def delete(self, task_id: int, user_id: int) -> None:
        row = self.get(task_id, user_id)
        with self.connection:
            self.connection.execute(
                "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            )
            if row["group_id"] is not None:
                self.connection.execute("UPDATE task_groups SET revision = revision + 1 WHERE id = ?", (row["group_id"],))

    def set_reminder(self, task_id: int, user_id: int, when: float | None) -> None:
        row = self.get(task_id, user_id)
        if when is not None and row["done"]:
            raise InputError("Задача уже выполнена. Напоминание можно задать для новой задачи: /add Текст.")
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET remind_at = ?, reminder_state = ?, next_attempt_at = ?, "
                "sent_at = NULL, revision = revision + 1 WHERE id = ? AND user_id = ?",
                (when, "none" if when is None else "pending", when, task_id, user_id),
            )

    def set_deadline(self, task_id: int, user_id: int, when: float | None, has_time: bool = True) -> None:
        self.get(task_id, user_id)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET deadline_at = ?, deadline_has_time = ?, revision = revision + 1 "
                "WHERE id = ? AND user_id = ?", (when, int(has_time), task_id, user_id),
            )

    def filtered_tasks(self, user_id: int, view: str, tz: ZoneInfo,
                       group_id: int | None = None, query: str | None = None,
                       now: datetime | None = None) -> list[sqlite3.Row]:
        """Готовые представления; UTC-сроки сравниваются с местным календарём."""
        if group_id is not None:
            self.get_group(group_id, user_id)
        rows = self.search(user_id, query) if query is not None else self.list_tasks(user_id, True, group_id)
        now = now or datetime.now(UTC)
        today = now.astimezone(tz).date()
        if view not in {"active", "done", "all", "high", "reminders", "today", "overdue"}:
            raise InputError("Неизвестный фильтр.")
        result = []
        for row in rows:
            if group_id is not None and row["group_id"] != group_id:
                continue
            if view == "done":
                keep = bool(row["done"])
            elif view == "all":
                keep = True
            elif row["done"]:
                keep = False
            elif view == "active":
                keep = True
            elif view == "high":
                keep = row["priority"] == 0
            elif view == "reminders":
                keep = row["reminder_state"] == "pending"
            elif view == "today":
                keep = row["deadline_at"] is not None and deadline_day(
                    row["deadline_at"], bool(row["deadline_has_time"]), tz,
                ) == today
            else:
                keep = row["deadline_at"] is not None and row["deadline_at"] <= now.timestamp()
            if keep:
                result.append(row)
        return result

    def due(self, now: float) -> list[sqlite3.Row]:
        return self.connection.execute(
            self.TASK_SELECT + "WHERE t.done = 0 AND t.reminder_state = 'pending' "
            "AND t.remind_at <= ? AND t.next_attempt_at <= ? ORDER BY t.remind_at, t.id LIMIT 100",
            (now, now),
        ).fetchall()

    def delivery_result(self, row: sqlite3.Row, state: str, retry_at: float | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET reminder_state = ?, next_attempt_at = ?, sent_at = ? "
                "WHERE id = ? AND user_id = ? AND reminder_state = 'pending' AND remind_at = ?",
                (state, retry_at, datetime.now(UTC).timestamp() if state == "sent" else None,
                 row["id"], row["user_id"], row["remind_at"]),
            )


def parse_id(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value) or not 0 < int(value) <= 2**63 - 1:
        raise InputError("ID должен быть положительным числом. Формат: /done <id>. Свои ID: /list.")
    return int(value)


def parse_reminder(value: str, tz: ZoneInfo, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    year = now.astimezone(tz).year
    example = f"Введите ДД.ММ ЧЧ:ММ, например 15.10 18:30 (год {year}, {tz.key}); укажите будущую дату. Можно указать год: ДД.ММ.ГГГГ ЧЧ:ММ."
    if re.fullmatch(r"[0-9]{2}\.[0-9]{2} [0-9]{2}:[0-9]{2}", value):
        value = f"{value[:5]}.{year} {value[6:]}"
    elif not re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4} [0-9]{2}:[0-9]{2}", value):
        raise InputError("Некорректный формат даты. " + example)
    try:
        local = datetime.strptime(value, "%d.%m.%Y %H:%M")
        aware = local.replace(tzinfo=tz)
        # Не принимаем отсутствующий/неоднозначный час при переводе часов в других зонах.
        if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != local:
            raise InputError("Такого местного времени нет из-за перевода часов. Выберите другое время.")
        if aware.utcoffset() != local.replace(tzinfo=tz, fold=1).utcoffset():
            raise InputError("Это время встречается дважды при переводе часов. Выберите другое время.")
        timestamp = aware.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("Некорректная дата или время. " + example) from exc
    if timestamp <= now.timestamp():
        raise InputError("Напоминание должно быть в будущем. " + example)
    return timestamp


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Делим также очень длинные строки, не разрывая Unicode-символы."""
    chunks, current, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


class TaskAssistant:
    # Эти правила используются и командным сервисом, и кнопочным интерфейсом.
    parse_id = staticmethod(parse_id)
    parse_reminder = staticmethod(parse_reminder)
    validate_text = staticmethod(validate_task_text)
    validate_group_name = staticmethod(validate_group_name)
    priorities = PRIORITIES
    priority_labels = PRIORITY_LABELS

    def __init__(self, store: TaskStore, tz: ZoneInfo):
        self.store = store
        self.tz = tz
        self.started_at = datetime.now(UTC).timestamp()
        # JobQueue и команды работают в одном цикле событий. Блокировка исключает
        # одновременное удаление/замену напоминания во время его отправки.
        self.lock = asyncio.Lock()
        self.drafts: dict[tuple[int, int], TaskDraft] = {}

    def format_time(self, timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, self.tz).strftime("%d.%m.%Y %H:%M") + f" ({self.tz.key})"

    def format_task(self, row: sqlite3.Row, show_created: bool = False) -> str:
        status = "✅ выполнена" if row["done"] else "⬜ не выполнена"
        text = f"#{row['id']} · {PRIORITY_LABELS[row['priority']]} · {status}\n{row['text']}"
        if row["description"]:
            text += f"\nОписание: {row['description']}"
        text += f"\nГруппа: {row['group_name'] or 'Без группы'}"
        if row["deadline_at"] is not None:
            when = row["deadline_at"]
            label = self.format_time(when) if row["deadline_has_time"] else (
                deadline_day(when, False, self.tz).strftime("%d.%m.%Y") + f" — до конца дня ({self.tz.key})"
            )
            overdue = not row["done"] and when <= datetime.now(UTC).timestamp()
            text += f"\n📅 Срок: {label}" + (" · ПРОСРОЧЕНА" if overdue else "")
        else:
            text += "\n📅 Срок: не задан"
        if show_created:
            text += f"\nСоздана: {self.format_time(row['created_at'])}"
        if row["remind_at"] is not None:
            states = {"pending": "ожидает отправки", "sent": "отправлено", "failed": "не доставлено; задайте заново через /remind"}
            text += f"\n⏰ {self.format_time(row['remind_at'])} — {states[row['reminder_state']]}"
        return text

    def respond(self, text: str | None, user_id: int, chat_id: int) -> str:
        """Обработка команды отдельно от Telegram упрощает проверку логики."""
        if text is None:
            return "Пожалуйста, пришлите задачу текстом. Список команд: /help."
        stripped = text.strip()
        if not stripped.startswith("/"):
            if (user_id, chat_id) in self.drafts:
                return self.advance_draft(stripped, user_id, chat_id)
            return self.add_response(user_id, chat_id, stripped)
        parts = stripped.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        body = parts[1] if len(parts) == 2 else ""
        args = body.split()
        if command in ("/start", "/help"):
            if command == "/start":
                self.drafts.pop((user_id, chat_id), None)
            intro = "Привет! Я помогу вести список дел. Используйте /add, чтобы создать задачу с описанием.\n\n" if command == "/start" else ""
            return intro + HELP + f"\nЧасовой пояс: {self.tz.key}."
        if command == "/add":
            if not body.strip():
                self.drafts[(user_id, chat_id)] = TaskDraft()
                return "Введите название задачи"
            return self.add_response(user_id, chat_id, body)
        if command == "/cancel":
            if args:
                raise InputError("Для отмены отправьте /cancel без параметров.")
            if self.drafts.pop((user_id, chat_id), None) is not None:
                return "Диалог отменён. Незавершённые изменения не сохранены."
            return "Нет активного диалога. Добавить задачу: /add."
        if command == "/search":
            rows = self.store.search(user_id, body)
            if not rows:
                return "Задачи не найдены. Попробуйте другую часть названия или описания."
            return "Найденные задачи:\n\n" + "\n\n".join(self.format_task(row, show_created=True) for row in rows)
        if command == "/edit":
            if len(args) != 1:
                raise InputError("Формат: /edit <id>.")
            task_id = parse_id(args[0])
            row = self.store.get(task_id, user_id)
            self.drafts[(user_id, chat_id)] = TaskDraft(
                mode="edit", target_id=task_id, step="edit_title", title=row["text"], description=row["description"],
            )
            return f"Текущее название: {row['text']}\nВведите новое название или нажмите «Оставить»."
        if command in ("/group_create", "/groups", "/group_tasks", "/group_rename", "/group_delete", "/task_group", "/task_ungroup"):
            return self.group_command(command, args, user_id, chat_id)
        if command in ("/list", "/list_all"):
            if args:
                raise InputError("Используйте /list для активных задач или /list_all для всех задач.")
            rows = self.store.list_tasks(user_id, include_done=command == "/list_all")
            if not rows:
                return "Список пуст. Добавьте задачу обычным сообщением или командой /add Купить продукты."
            return "Ваши задачи:\n\n" + "\n\n".join(self.format_task(row) for row in rows)
        if command in ("/done", "/delete"):
            if len(args) != 1:
                raise InputError(f"Нужен ID задачи. Формат: {command} <id>.")
            task_id = parse_id(args[0])
            if command == "/done":
                self.store.finish(task_id, user_id)
                return f"✅ Задача #{task_id} выполнена. Напоминание отменено."
            self.store.delete(task_id, user_id)
            return f"Задача #{task_id} удалена вместе с напоминанием."
        if command == "/priority":
            if len(args) != 2 or args[1].lower() not in PRIORITIES:
                raise InputError("Приоритет: высокий, средний или низкий. Формат: /priority <id> высокий.")
            task_id, priority = parse_id(args[0]), PRIORITIES[args[1].lower()]
            self.store.set_priority(task_id, user_id, priority)
            return f"Приоритет задачи #{task_id}: {PRIORITY_LABELS[priority]}."
        if command == "/remind_off":
            if len(args) != 1:
                raise InputError("Нужен ID задачи. Формат: /remind_off <id>.")
            task_id = parse_id(args[0])
            self.store.set_reminder(task_id, user_id, None)
            return f"Напоминание для задачи #{task_id} отменено."
        if command == "/remind":
            if len(args) != 3:
                raise InputError("Формат: /remind <id> ДД.ММ ЧЧ:ММ. Отмена: /remind_off <id>.")
            task_id = parse_id(args[0])
            self.store.get(task_id, user_id)
            when = parse_reminder(" ".join(args[1:]), self.tz)
            self.store.set_reminder(task_id, user_id, when)
            return f"⏰ Напоминание для задачи #{task_id}: {self.format_time(when)}."
        return "Неизвестная команда. Посмотрите примеры: /help."

    def group_command(self, command: str, args: list[str], user_id: int, chat_id: int) -> str:
        if command in ("/group_create", "/groups"):
            if args:
                raise InputError(f"Отправьте {command} без параметров.")
            if command == "/group_create":
                self.drafts[(user_id, chat_id)] = TaskDraft(mode="group_create", step="group_name")
                return "Введите название группы"
            rows = self.store.list_groups(user_id)
            if not rows:
                return "Групп пока нет. Создать группу: /group_create."
            return "Ваши группы:\n" + "\n".join(
                f"#{row['id']} · {row['name']} — активных задач: {row['active_count']}" for row in rows
            )
        if len(args) != 1:
            placeholder = "<id>" if command.startswith("/task_") else "<group_id>"
            raise InputError(f"Формат: {command} {placeholder}.")
        entity_id = parse_id(args[0])
        if command.startswith("/task_"):
            self.store.get(entity_id, user_id)
            if command == "/task_ungroup":
                self.store.assign_group(entity_id, user_id, None)
                return f"Задача #{entity_id} теперь без группы."
            if not self.store.list_groups(user_id):
                return "Групп пока нет. Сначала создайте группу: /group_create."
            self.drafts[(user_id, chat_id)] = TaskDraft(mode="assign_group", target_id=entity_id, step="select_group")
            return "Выберите группу кнопкой или введите её ID. Отмена: /cancel."
        row = self.store.get_group(entity_id, user_id)
        if command == "/group_tasks":
            tasks = self.store.list_tasks(user_id, include_done=True, group_id=entity_id)
            if not tasks:
                return f"В группе «{row['name']}» пока нет задач. Добавить задачу в неё: /task_group <id>."
            return f"Задачи группы «{row['name']}»:\n\n" + "\n\n".join(self.format_task(task) for task in tasks)
        if command == "/group_rename":
            self.drafts[(user_id, chat_id)] = TaskDraft(mode="group_rename", target_id=entity_id, step="group_name")
            return f"Текущее название группы: {row['name']}\nВведите новое название группы"
        self.store.delete_group(entity_id, user_id)
        return f"Группа «{row['name']}» удалена. Её задачи сохранены без группы, напоминания не изменены."

    def resolve_group_choice(self, text: str, user_id: int) -> sqlite3.Row:
        # Кнопка содержит ID, чтобы названия «Пропустить» и одинаковые имена
        # разных пользователей не путали выбор. Владельца проверяем заново.
        match = re.fullmatch(r"#([0-9]+) · .+", text)
        group_id = parse_id(match.group(1) if match else text)
        return self.store.get_group(group_id, user_id)

    def reminder_prompt(self) -> str:
        year = datetime.now(self.tz).year
        return (
            "Установить напоминание\nВведите дату и время в формате ДД.ММ ЧЧ:ММ, "
            f"например 15.10 18:30. Год: {year}, часовой пояс: {self.tz.key}.\n"
            "Дата должна быть в будущем. Если напоминание не нужно, нажмите «Пропустить»."
        )

    def advance_draft(self, text: str, user_id: int, chat_id: int) -> str:
        draft = self.drafts[(user_id, chat_id)]
        if draft.mode in ("group_create", "group_rename"):
            if draft.mode == "group_create":
                row = self.store.create_group(user_id, text)
                response = f"Группа создана: #{row['id']} · {row['name']}. Добавить задачу: /task_group <id>."
            else:
                self.store.rename_group(draft.target_id, user_id, text)
                response = f"Группа #{draft.target_id} переименована в «{text.strip()}»."
            self.drafts.pop((user_id, chat_id))
            return response
        if draft.mode == "assign_group":
            group = self.resolve_group_choice(text, user_id)
            self.store.assign_group(draft.target_id, user_id, group["id"])
            self.drafts.pop((user_id, chat_id))
            return f"Задача #{draft.target_id} в группе «{group['name']}»."
        if draft.mode == "edit":
            if draft.step == "edit_title":
                title = draft.title if text.casefold() == "оставить" else text
                validate_task_text(title)
                draft.title, draft.step = title, "edit_description"
                current = draft.description or "(без описания)"
                return f"Текущее описание: {current}\nВведите новое описание, нажмите «Оставить» или «Очистить»."
            description = draft.description if text.casefold() == "оставить" else "" if text.casefold() == "очистить" else text
            self.store.update_text(draft.target_id, user_id, draft.title, description)
            row = self.store.get(draft.target_id, user_id)
            self.drafts.pop((user_id, chat_id))
            return "Задача обновлена!\n" + self.format_task(row)
        if draft.step == "title":
            validate_task_text(text)
            draft.title, draft.step = text, "description"
            return "Введите описание\nОписание необязательно — нажмите «Пропустить»."
        if draft.step == "description":
            description = "" if text.lower() == "пропустить" else text
            validate_task_text(draft.title, description)
            draft.description, draft.step = description, "priority"
            return "Укажите приоритет (высокий, средний, низкий)"
        if draft.step == "priority":
            if text.lower() not in PRIORITIES:
                raise InputError("Укажите приоритет (высокий, средний, низкий) — выберите кнопку или введите вариант текстом.")
            # Сначала читаем базу: при ошибке выбранный шаг остаётся доступен.
            groups = self.store.list_groups(user_id)
            draft.priority = PRIORITIES[text.lower()]
            draft.step = "add_group" if groups else "reminder"
            if groups:
                return "Выберите группу для задачи или нажмите «Пропустить». Можно ввести ID группы."
            return self.reminder_prompt()
        if draft.step == "add_group":
            group_id = None if text.casefold() == "пропустить" else self.resolve_group_choice(text, user_id)["id"]
            draft.group_id, draft.step = group_id, "reminder"
            return self.reminder_prompt()
        when = None if text.lower() == "пропустить" else parse_reminder(text, self.tz)
        if draft.group_id is not None:
            try:
                self.store.get_group(draft.group_id, user_id)
            except InputError as exc:
                draft.group_id, draft.step = None, "add_group"
                raise InputError("Выбранная группа удалена. Выберите другую группу или нажмите «Пропустить».") from exc
        # Единственная запись в базу происходит в конце. Если SQLite выдаст ошибку,
        # черновик останется на последнем шаге для повторной попытки.
        return self.add_response(user_id, chat_id, draft.title, draft.description, draft.priority, when, draft.group_id)

    def keyboard(self, user_id: int, chat_id: int) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
        draft = self.drafts.get((user_id, chat_id))
        if draft and draft.step == "edit_title":
            return ReplyKeyboardMarkup([["Оставить"]], resize_keyboard=True)
        if draft and draft.step == "edit_description":
            return ReplyKeyboardMarkup([["Оставить", "Очистить"]], resize_keyboard=True)
        if draft and draft.step in ("select_group", "add_group"):
            groups = self.store.list_groups(user_id)
            # До 50 кнопок; остальные группы доступны через /groups и ввод ID.
            keys = [[f"#{row['id']} · {row['name']}"] for row in groups[:50]]
            if draft.step == "add_group":
                keys.append(["Пропустить"])
            return ReplyKeyboardMarkup(keys, resize_keyboard=True) if keys else ReplyKeyboardRemove()
        if draft and draft.step == "priority":
            return ReplyKeyboardMarkup([["высокий", "средний", "низкий"]], resize_keyboard=True)
        if draft and draft.step in ("description", "reminder"):
            return ReplyKeyboardMarkup([["Пропустить"]], resize_keyboard=True)
        return ReplyKeyboardRemove()

    def add_response(
        self, user_id: int, chat_id: int, text: str, description: str = "",
        priority: int = 1, remind_at: float | None = None,
        group_id: int | None = None,
    ) -> str:
        row = self.store.add(user_id, chat_id, text, description, priority, remind_at, group_id)
        self.drafts.pop((user_id, chat_id), None)
        return (
            "Задача добавлена!\n" + self.format_task(row)
            + ("\nНапоминание не установлено." if remind_at is None else "")
            + f"\n\nНапоминание: /remind {row['id']} ДД.ММ ЧЧ:ММ"
            + f"\nПриоритет: /priority {row['id']} высокий"
        )

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if message is None or chat is None:
            return
        if chat.type != "private":
            await message.reply_text("Я веду личные списки задач. Напишите мне в личный чат.")
            return
        if user is None:
            return
        if message.text and message.text.startswith("/"):
            command = message.text.split(maxsplit=1)[0]
            if "@" in command and command.split("@", 1)[1].lower() != context.bot.username.lower():
                # Общий обработчик неизвестных команд не должен выполнять команды другого бота.
                return
        try:
            async with self.lock:
                response = self.respond(message.text, user.id, chat.id)
        except InputError as exc:
            response = str(exc)
        except sqlite3.Error:
            LOGGER.exception("Ошибка SQLite при обработке команды")
            response = "Не удалось прочитать или сохранить данные. Попробуйте позже; проверьте результат через /list_all."
        try:
            markup = self.keyboard(user.id, chat.id)
        except sqlite3.Error:
            # Даже если база недоступна для построения клавиатуры, отправляем ошибку.
            LOGGER.exception("Не удалось прочитать группы для клавиатуры")
            markup = ReplyKeyboardRemove()
        for chunk in split_message(response):
            await message.reply_text(chunk, reply_markup=markup)

    async def send_due(self, telegram_bot) -> None:
        async with self.lock:
            now = datetime.now(UTC).timestamp()
            for row in self.store.due(now):
                delayed = row["remind_at"] < self.started_at or now - row["remind_at"] > 60
                title = "⏰ Напоминание с задержкой" if delayed else "⏰ Напоминание"
                description = f"Описание: {row['description']}\n" if row["description"] else ""
                text = (
                    f"{title}: задача #{row['id']}\n{row['text']}\n"
                    f"{description}"
                    f"Группа: {row['group_name'] or 'Без группы'}\n"
                    f"{PRIORITY_LABELS[row['priority']]}\n"
                    f"Запланировано: {self.format_time(row['remind_at'])}\n"
                    f"Выполнить: /done {row['id']}"
                )
                try:
                    await telegram_bot.send_message(chat_id=row["chat_id"], text=text,
                                                    reply_markup=self.reminder_markup(row))
                except RetryAfter as exc:
                    delay = exc.retry_after
                    seconds = delay.total_seconds() if isinstance(delay, timedelta) else delay
                    self.store.delivery_result(row, "pending", datetime.now(UTC).timestamp() + seconds + 1)
                    # Лимит Telegram может относиться ко всему боту, прекращаем пакет.
                    break
                except (Forbidden, BadRequest):
                    LOGGER.exception("Неустранимая ошибка доставки напоминания #%s", row["id"])
                    self.store.delivery_result(row, "failed")
                except TelegramError:
                    LOGGER.exception("Временная ошибка доставки напоминания #%s", row["id"])
                    self.store.delivery_result(row, "pending", datetime.now(UTC).timestamp() + 60)
                else:
                    # Отмечаем отправленным только после подтверждения Telegram.
                    self.store.delivery_result(row, "sent")

    def reminder_markup(self, row):
        """Telegram-адаптер новой версии подставляет кнопки; сервис не знает их устройства."""
        return None


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.application.bot_data["assistant"].send_due(context.bot)


async def post_init(application: Application) -> None:
    application.job_queue.run_repeating(
        reminder_job, interval=CHECK_INTERVAL, first=1, name="sqlite-reminders",
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Начать работу"), BotCommand("menu", "Главное меню"), BotCommand("help", "Помощь"),
            BotCommand("add", "Добавить задачу"), BotCommand("list", "Список задач"),
            BotCommand("list_all", "Все задачи, включая выполненные"),
            BotCommand("search", "Поиск по части названия и описания"), BotCommand("edit", "Изменить задачу"),
            BotCommand("group_create", "Создать группу"), BotCommand("groups", "Мои группы"),
            BotCommand("group_tasks", "Задачи группы"), BotCommand("group_rename", "Переименовать группу"),
            BotCommand("group_delete", "Удалить группу, сохранив задачи"),
            BotCommand("task_group", "Выбрать группу задачи"), BotCommand("task_ungroup", "Убрать задачу из группы"),
            BotCommand("priority", "Изменить приоритет"), BotCommand("done", "Выполнить задачу"),
            BotCommand("delete", "Удалить задачу"), BotCommand("remind", "Настроить напоминание"),
            BotCommand("remind_off", "Отменить напоминание"), BotCommand("cancel", "Отменить текущий диалог"),
        ])
    except TelegramError:
        LOGGER.exception("Не удалось обновить меню команд; обработчики команд доступны")
    LOGGER.info("Бот запущен. Для остановки нажмите Ctrl+C.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    LOGGER.error("Ошибка обработки Telegram/JobQueue", exc_info=(type(error), error, error.__traceback__))
    if isinstance(update, Update) and update.effective_message and update.effective_chat.type == "private":
        try:
            await update.effective_message.reply_text(
                "Произошла техническая ошибка. Проверьте результат через /list_all перед повтором команды."
            )
        except TelegramError:
            LOGGER.warning("Не удалось доставить сообщение об ошибке")


def build_application(settings: Settings, store: TaskStore) -> Application:
    # Импорт не создаёт соединений, не читает .env и не меняет базу.
    from buttons import ButtonAssistant
    assistant = ButtonAssistant(TaskAssistant(store, settings.timezone))
    application = Application.builder().token(settings.token).post_init(post_init).build()
    application.bot_data["assistant"] = assistant
    # Отдельный фильтр для обычных сообщений исключает повторное создание задач
    # при редактировании уже отправленного текста.
    commands = [
        "start", "menu", "help", "add", "list", "list_all", "priority", "done", "delete", "remind", "remind_off", "cancel",
        "search", "edit", "group_create", "groups", "group_tasks", "group_rename", "group_delete", "task_group", "task_ungroup",
    ]
    application.add_handler(CommandHandler(commands, assistant.handle, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CallbackQueryHandler(assistant.handle_callback))
    application.add_handler(MessageHandler(filters.UpdateType.MESSAGE, assistant.handle))
    application.add_error_handler(on_error)
    return application


def main() -> int:
    configure_logging()
    store = None
    try:
        settings = Settings.load()
        store = TaskStore(settings.database)
        application = build_application(settings, store)
        application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
    except InputError as exc:
        LOGGER.error("%s", exc)
        return 1
    except InvalidToken:
        LOGGER.error("Telegram отклонил токен. Проверьте TELEGRAM_BOT_TOKEN в lab1/.env.")
        return 1
    except (sqlite3.Error, OSError):
        LOGGER.exception("Не удалось открыть базу или запустить бота. Проверьте DATABASE_PATH, права и сеть.")
        return 1
    except TelegramError:
        LOGGER.exception("Ошибка подключения к Telegram. Проверьте сеть и отсутствие второй копии бота.")
        return 1
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
