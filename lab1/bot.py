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
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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
/priority <id> высокий — изменить приоритет (высокий, средний, низкий)
/done <id> — выполнить задачу и отменить напоминание
/delete <id> — удалить задачу
/remind <id> 15.10 18:30 — установить или заменить напоминание
/remind_off <id> — отменить напоминание
/cancel — отменить пошаговое создание
/help — помощь

В примерах замените <id> на ID своей задачи, а дату — на будущую.
Формат даты: ДД.ММ ЧЧ:ММ (текущий год), также доступен ДД.ММ.ГГГГ ЧЧ:ММ.
"""


class InputError(ValueError):
    """Ошибка ввода, которую можно безопасно показать пользователю."""


@dataclass
class TaskDraft:
    """Незавершённая задача; отдельный черновик для каждого личного чата."""

    step: str = "title"
    title: str = ""
    description: str = ""
    priority: int = 1


def validate_task_text(title: str, description: str = "") -> None:
    if not title.strip():
        raise InputError("Название не должно быть пустым. Введите название задачи.")
    # Общий лимит оставляет место для служебных строк в сообщениях Telegram.
    if len((title + description).encode("utf-16-le")) // 2 > 3000:
        raise InputError("Сократите название и описание: вместе не более 3000 символов (эмодзи могут занимать два).")


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

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            with self.connection:
                self.connection.executescript("""
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
                        sent_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(user_id);
                    CREATE INDEX IF NOT EXISTS tasks_reminders
                        ON tasks(reminder_state, next_attempt_at);
                """)
                # CREATE TABLE не меняет существующую схему. Миграция добавляет
                # только описание, сохраняя старые ID, статусы и напоминания.
                columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)")}
                if "description" not in columns:
                    self.connection.execute("ALTER TABLE tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        except sqlite3.Error:
            self.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def get(self, task_id: int, user_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        ).fetchone()
        if row is None:
            raise InputError("Задача не найдена. Посмотрите ID своих задач: /list_all.")
        return row

    def add(
        self, user_id: int, chat_id: int, text: str, description: str = "",
        priority: int = 1, remind_at: float | None = None,
    ) -> sqlite3.Row:
        text = text.strip()
        description = description.strip()
        validate_task_text(text, description)
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO tasks(user_id, chat_id, text, description, priority, created_at, "
                "remind_at, reminder_state, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, chat_id, text, description, priority, datetime.now(UTC).timestamp(),
                 remind_at, "pending" if remind_at is not None else "none", remind_at),
            )
        return self.get(cursor.lastrowid, user_id)

    def list_tasks(self, user_id: int, include_done: bool = False) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND (? OR done = 0) "
            "ORDER BY priority, created_at, id", (user_id, include_done)
        ).fetchall()

    def set_priority(self, task_id: int, user_id: int, priority: int) -> None:
        self.get(task_id, user_id)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET priority = ? WHERE id = ? AND user_id = ?",
                (priority, task_id, user_id),
            )

    def finish(self, task_id: int, user_id: int) -> None:
        self.get(task_id, user_id)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET done = 1, remind_at = NULL, reminder_state = 'none', "
                "next_attempt_at = NULL, sent_at = NULL WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )

    def delete(self, task_id: int, user_id: int) -> None:
        self.get(task_id, user_id)
        with self.connection:
            self.connection.execute(
                "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
            )

    def set_reminder(self, task_id: int, user_id: int, when: float | None) -> None:
        row = self.get(task_id, user_id)
        if when is not None and row["done"]:
            raise InputError("Задача уже выполнена. Напоминание можно задать для новой задачи: /add Текст.")
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET remind_at = ?, reminder_state = ?, next_attempt_at = ?, "
                "sent_at = NULL WHERE id = ? AND user_id = ?",
                (when, "none" if when is None else "pending", when, task_id, user_id),
            )

    def due(self, now: float) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM tasks WHERE done = 0 AND reminder_state = 'pending' "
            "AND remind_at <= ? AND next_attempt_at <= ? ORDER BY remind_at, id LIMIT 100",
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

    def format_task(self, row: sqlite3.Row) -> str:
        status = "✅ выполнена" if row["done"] else "⬜ не выполнена"
        text = f"#{row['id']} · {PRIORITY_LABELS[row['priority']]} · {status}\n{row['text']}"
        if row["description"]:
            text += f"\nОписание: {row['description']}"
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
                return "Создание задачи отменено. Чтобы начать заново, отправьте /add."
            return "Нет задачи в процессе создания. Начать: /add."
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

    def advance_draft(self, text: str, user_id: int, chat_id: int) -> str:
        draft = self.drafts[(user_id, chat_id)]
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
            draft.priority, draft.step = PRIORITIES[text.lower()], "reminder"
            year = datetime.now(self.tz).year
            return (
                "Установить напоминание\nВведите дату и время в формате ДД.ММ ЧЧ:ММ, "
                f"например 15.10 18:30. Год: {year}, часовой пояс: {self.tz.key}.\n"
                "Дата должна быть в будущем. Если напоминание не нужно, нажмите «Пропустить»."
            )
        when = None if text.lower() == "пропустить" else parse_reminder(text, self.tz)
        # Единственная запись в базу происходит в конце. Если SQLite выдаст ошибку,
        # черновик останется на последнем шаге для повторной попытки.
        return self.add_response(user_id, chat_id, draft.title, draft.description, draft.priority, when)

    def keyboard(self, user_id: int, chat_id: int) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
        draft = self.drafts.get((user_id, chat_id))
        if draft and draft.step == "priority":
            return ReplyKeyboardMarkup([["высокий", "средний", "низкий"]], resize_keyboard=True)
        if draft and draft.step in ("description", "reminder"):
            return ReplyKeyboardMarkup([["Пропустить"]], resize_keyboard=True)
        return ReplyKeyboardRemove()

    def add_response(
        self, user_id: int, chat_id: int, text: str, description: str = "",
        priority: int = 1, remind_at: float | None = None,
    ) -> str:
        row = self.store.add(user_id, chat_id, text, description, priority, remind_at)
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
        for chunk in split_message(response):
            await message.reply_text(chunk, reply_markup=self.keyboard(user.id, chat.id))

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
                    f"{PRIORITY_LABELS[row['priority']]}\n"
                    f"Запланировано: {self.format_time(row['remind_at'])}\n"
                    f"Выполнить: /done {row['id']}"
                )
                try:
                    await telegram_bot.send_message(chat_id=row["chat_id"], text=text)
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


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.application.bot_data["assistant"].send_due(context.bot)


async def post_init(application: Application) -> None:
    application.job_queue.run_repeating(
        reminder_job, interval=CHECK_INTERVAL, first=1, name="sqlite-reminders",
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Начать работу"), BotCommand("help", "Помощь"),
            BotCommand("add", "Добавить задачу"), BotCommand("list", "Список задач"),
            BotCommand("list_all", "Все задачи, включая выполненные"),
            BotCommand("priority", "Изменить приоритет"), BotCommand("done", "Выполнить задачу"),
            BotCommand("delete", "Удалить задачу"), BotCommand("remind", "Настроить напоминание"),
            BotCommand("remind_off", "Отменить напоминание"), BotCommand("cancel", "Отменить создание задачи"),
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
    assistant = TaskAssistant(store, settings.timezone)
    application = Application.builder().token(settings.token).post_init(post_init).build()
    application.bot_data["assistant"] = assistant
    # Отдельный фильтр для обычных сообщений исключает повторное создание задач
    # при редактировании уже отправленного текста.
    commands = ["start", "help", "add", "list", "list_all", "priority", "done", "delete", "remind", "remind_off", "cancel"]
    application.add_handler(CommandHandler(commands, assistant.handle, filters=filters.UpdateType.MESSAGE))
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
        application.run_polling(allowed_updates=["message"], drop_pending_updates=False)
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
