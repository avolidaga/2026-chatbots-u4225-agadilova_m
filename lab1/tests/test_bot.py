"""Проверки без настоящего токена и без запросов к Telegram."""

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.request import BaseRequest

from bot import (
    BASE_DIR, UTC, InputError, SafeFormatter, Settings, TaskAssistant, TaskStore,
    build_application, main, parse_id, parse_reminder, post_init, split_message,
)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.assistant = TaskAssistant(self.store, ZoneInfo("Europe/Moscow"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_add_defaults_and_multiline_text(self):
        reply = self.assistant.respond("/add  Купить хлеб\nи молоко  ", 1, 1)
        row = self.store.list_tasks(1)[0]
        self.assertEqual(row["text"], "Купить хлеб\nи молоко")
        self.assertEqual(row["priority"], 1)
        self.assertEqual(row["done"], 0)
        self.assertIn(f"/remind {row['id']}", reply)

    def test_empty_and_oversize_tasks_are_rejected(self):
        for text in ("", " \n ", "😀" * 1501):
            with self.subTest(text=text[:20]), self.assertRaises(InputError):
                self.assistant.respond(text, 1, 1)
        self.assertEqual(self.store.list_tasks(1), [])

    def test_priority_sort_and_creation_order(self):
        ids = [self.store.add(1, 1, text)["id"] for text in ("a", "b", "c", "d")]
        self.store.set_priority(ids[0], 1, 2)
        self.assistant.respond(f"/priority {ids[2]} высокий", 1, 1)
        self.store.set_priority(ids[3], 1, 0)
        self.assertEqual([r["id"] for r in self.store.list_tasks(1)], [ids[2], ids[3], ids[1], ids[0]])

    def test_ownership_on_every_id_command(self):
        row = self.store.add(1, 1, "Личная задача")
        task_id = row["id"]
        for command in (
            f"/priority {task_id} высокий", f"/done {task_id}", f"/delete {task_id}",
            f"/remind {task_id} 01.01.2099 12:00", f"/remind_off {task_id}",
        ):
            with self.subTest(command=command), self.assertRaisesRegex(InputError, "не найдена"):
                self.assistant.respond(command, 2, 2)
        self.assertEqual(self.store.list_tasks(2, True), [])
        self.assertNotIn("Личная задача", self.assistant.respond("/list_all", 2, 2))
        self.assertEqual(dict(row), dict(self.store.get(task_id, 1)))

    def test_reminder_replacement_cancellation_and_completion(self):
        task_id = self.store.add(1, 1, "Позвонить")["id"]
        reply = self.assistant.respond(f"/remind {task_id} 01.01.2099 12:00", 1, 1)
        self.assertIn("01.01.2099 12:00 (Europe/Moscow)", reply)
        first = self.store.get(task_id, 1)["remind_at"]
        self.assistant.respond(f"/remind {task_id} 01.01.2099 13:00", 1, 1)
        self.assertEqual(self.store.get(task_id, 1)["remind_at"], first + 3600)
        self.assistant.respond(f"/remind_off {task_id}", 1, 1)
        self.assertIsNone(self.store.get(task_id, 1)["remind_at"])
        self.assistant.respond(f"/remind {task_id} 01.01.2099 13:00", 1, 1)
        self.assistant.respond(f"/done {task_id}", 1, 1)
        self.assertEqual(self.store.list_tasks(1), [])
        self.assertEqual(len(self.store.list_tasks(1, True)), 1)
        self.assertEqual(self.store.get(task_id, 1)["reminder_state"], "none")
        with self.assertRaisesRegex(InputError, "выполнена"):
            self.assistant.respond(f"/remind {task_id} 01.01.2099 13:00", 1, 1)
        self.assistant.respond(f"/delete {task_id}", 1, 1)
        self.assertEqual(self.store.list_tasks(1, True), [])

    def test_wizard_all_fields_saved_only_at_the_end(self):
        self.assertEqual(self.assistant.respond("/add", 1, 1), "Введите название задачи")
        self.assertIn("Введите описание", self.assistant.respond("Подготовить отчёт", 1, 1))
        self.assertIn("Укажите приоритет", self.assistant.respond("Добавить скриншоты\nи выводы", 1, 1))
        self.assertIsInstance(self.assistant.keyboard(1, 1), ReplyKeyboardMarkup)
        keys = [button.text for row in self.assistant.keyboard(1, 1).keyboard for button in row]
        self.assertEqual(keys, ["высокий", "средний", "низкий"])
        reminder_prompt = self.assistant.respond("высокий", 1, 1)
        self.assertIn("Установить напоминание", reminder_prompt)
        self.assertIn("ДД.ММ ЧЧ:ММ", reminder_prompt)
        self.assertIn("Europe/Moscow", reminder_prompt)
        self.assertEqual(self.store.list_tasks(1), [])
        reply = self.assistant.respond("15.10.2099 18:30", 1, 1)
        row = self.store.list_tasks(1)[0]
        self.assertEqual(row["text"], "Подготовить отчёт")
        self.assertEqual(row["description"], "Добавить скриншоты\nи выводы")
        self.assertEqual(row["priority"], 0)
        self.assertEqual(row["reminder_state"], "pending")
        self.assertEqual(row["remind_at"], row["next_attempt_at"])
        self.assertIn("Описание:", reply)
        self.assertIn("2099", reply)
        self.assertIsInstance(self.assistant.keyboard(1, 1), ReplyKeyboardRemove)
        self.assertNotIn((1, 1), self.assistant.drafts)
        self.assertIn(row["description"], self.assistant.respond("/list", 1, 1))
        self.store.close()
        self.store = TaskStore(self.path)
        self.assertEqual(self.store.get(row["id"], 1)["description"], row["description"])

    def test_wizard_optional_fields_can_be_skipped(self):
        self.assistant.respond("/add  ", 1, 1)
        self.assistant.respond("Прогулка", 1, 1)
        self.assistant.respond("Пропустить", 1, 1)
        self.assistant.respond("низкий", 1, 1)
        self.assistant.respond("Пропустить", 1, 1)
        row = self.store.list_tasks(1)[0]
        self.assertEqual(row["description"], "")
        self.assertEqual(row["priority"], 2)
        self.assertEqual(row["reminder_state"], "none")
        self.assertIsNone(row["remind_at"])

    def test_wizard_invalid_inputs_keep_current_step(self):
        self.assistant.respond("/add", 1, 1)
        for text in ("  ", "😀" * 1501):
            with self.subTest(text=text[:10]), self.assertRaises(InputError):
                self.assistant.respond(text, 1, 1)
            self.assertEqual(self.assistant.drafts[(1, 1)].step, "title")
        self.assistant.respond("Название", 1, 1)
        with self.assertRaises(InputError):
            self.assistant.respond("😀" * 1500, 1, 1)
        self.assertEqual(self.assistant.drafts[(1, 1)].step, "description")
        self.assistant.respond("Описание", 1, 1)
        with self.assertRaises(InputError):
            self.assistant.respond("срочный", 1, 1)
        self.assertEqual(self.assistant.drafts[(1, 1)].step, "priority")
        self.assistant.respond("средний", 1, 1)
        for text in ("завтра", "31.02 12:00", "01.01.2000 00:00"):
            with self.subTest(text=text), self.assertRaises(InputError):
                self.assistant.respond(text, 1, 1)
            self.assertEqual(self.assistant.drafts[(1, 1)].step, "reminder")
        self.assertEqual(self.store.list_tasks(1), [])
        self.assistant.respond("Пропустить", 1, 1)
        self.assertEqual(self.store.list_tasks(1)[0]["description"], "Описание")

    def test_wizard_cancel_restart_and_other_commands(self):
        self.assistant.respond("/add", 1, 1)
        self.assistant.respond("Черновик", 1, 1)
        for command in ("/help", "/list", "/list_all"):
            self.assistant.respond(command, 1, 1)
            self.assertEqual(self.assistant.drafts[(1, 1)].step, "description")
        self.assistant.respond("/add", 1, 1)
        self.assertEqual(self.assistant.drafts[(1, 1)].step, "title")
        self.assistant.respond("/cancel", 1, 1)
        self.assertNotIn((1, 1), self.assistant.drafts)
        self.assertEqual(self.store.list_tasks(1), [])
        self.assistant.respond("/add", 1, 1)
        self.assistant.respond("/start", 1, 1)
        self.assertNotIn((1, 1), self.assistant.drafts)

    def test_wizard_users_are_independent(self):
        self.assistant.respond("/add", 1, 1)
        self.assistant.respond("/add", 2, 2)
        self.assistant.respond("Первая", 1, 1)
        self.assistant.respond("Вторая", 2, 2)
        self.assistant.respond("Первое описание", 1, 1)
        self.assistant.respond("Пропустить", 2, 2)
        self.assistant.respond("высокий", 1, 1)
        self.assistant.respond("низкий", 2, 2)
        self.assistant.respond("Пропустить", 2, 2)
        self.assistant.respond("Пропустить", 1, 1)
        self.assertEqual(self.store.list_tasks(1)[0]["description"], "Первое описание")
        self.assertEqual(self.store.list_tasks(2)[0]["text"], "Вторая")
        self.assertEqual(self.store.list_tasks(2)[0]["description"], "")

    def test_help_new_commands_and_legacy_syntax(self):
        help_text = self.assistant.respond("/help", 1, 1)
        for command in ("/add", "/list_all", "/remind_off <id>", "/cancel", "/done <id>"):
            self.assertIn(command, help_text)
        self.assertNotRegex(help_text, r"/(?:priority|done|delete|remind|remind_off) [0-9]")
        self.assertNotIn("/list all", help_text)
        with self.assertRaisesRegex(InputError, "/list_all"):
            self.assistant.respond("/list all", 1, 1)
        with self.assertRaisesRegex(InputError, "/remind_off"):
            self.assistant.respond("/remind 1 off", 1, 1)
        self.store.add(1, 1, "Выполненная")
        self.assistant.respond("/done 1", 1, 1)
        self.assertIn("Список пуст", self.assistant.respond("/list", 1, 1))
        self.assertIn("Выполненная", self.assistant.respond("/list_all", 1, 1))

    def test_migration_of_previous_schema_preserves_all_old_fields(self):
        task_id = self.store.add(1, 1, "Старая задача")["id"]
        self.store.set_priority(task_id, 1, 0)
        self.store.set_reminder(task_id, 1, datetime.now(UTC).timestamp() + 600)
        before = dict(self.store.get(task_id, 1))
        before.pop("description")
        # Временная тестовая база приводится к схеме первой версии проекта.
        self.store.connection.execute("ALTER TABLE tasks DROP COLUMN description")
        self.store.connection.commit()
        self.store.close()
        self.store = TaskStore(self.path)
        after = dict(self.store.get(task_id, 1))
        self.assertEqual(after.pop("description"), "")
        self.assertEqual(before, after)
        self.store.close()
        self.store = TaskStore(self.path)
        self.assertEqual(len(self.store.list_tasks(1)), 1)
        new_row = self.store.add(1, 1, "Новая", description="После миграции")
        self.assertEqual(new_row["description"], "После миграции")
        self.assertGreater(new_row["id"], task_id)

    def test_restart_preserves_tasks_and_due_reminders(self):
        row = self.store.add(1, 1, "Сохранить")
        self.store.set_reminder(row["id"], 1, datetime.now(UTC).timestamp() - 60)
        self.store.close()
        self.store = TaskStore(self.path)
        self.assertEqual(self.store.get(row["id"], 1)["text"], "Сохранить")
        self.assertEqual(len(self.store.due(datetime.now(UTC).timestamp())), 1)

    def test_unknown_ids_and_invalid_commands(self):
        for command in ("/list bad", "/list_all bad", "/done", "/delete 1 2", "/priority 1 urgent", "/remind", "/remind_off", "/done 999"):
            with self.subTest(command=command), self.assertRaises(InputError):
                self.assistant.respond(command, 1, 1)
        self.assertIn("/help", self.assistant.respond("/unknown", 1, 1))
        self.assertIn("текстом", self.assistant.respond(None, 1, 1))
        self.assertIn("Europe/Moscow", self.assistant.respond("/start", 1, 1))

    def test_long_list_is_split_without_lost_text(self):
        for _ in range(8):
            self.store.add(1, 1, "😀 Задача\n" * 200)
        text = self.assistant.respond("/list", 1, 1)
        chunks = split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 4000 for c in chunks))


class ValidationCase(unittest.TestCase):
    def test_short_date_uses_current_local_year(self):
        tz = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 10, 1, 12, tzinfo=UTC)
        result = datetime.fromtimestamp(parse_reminder("15.10 18:30", tz, now), tz)
        self.assertEqual(result, datetime(2026, 10, 15, 18, 30, tzinfo=tz))
        with self.assertRaisesRegex(InputError, "в будущем"):
            parse_reminder("01.01 12:00", tz, now)
        # В UTC ещё 2026 год, но в Москве уже 2027.
        utc_new_year = datetime(2026, 12, 31, 22, tzinfo=UTC)
        result = datetime.fromtimestamp(parse_reminder("01.01 03:00", tz, utc_new_year), tz)
        self.assertEqual(result.year, 2027)

    def test_short_leap_date(self):
        tz = ZoneInfo("Europe/Moscow")
        leap = parse_reminder("29.02 12:00", tz, datetime(2028, 2, 1, tzinfo=UTC))
        self.assertEqual(datetime.fromtimestamp(leap, tz).day, 29)
        with self.assertRaisesRegex(InputError, "Некорректная дата"):
            parse_reminder("29.02 12:00", tz, datetime(2027, 2, 1, tzinfo=UTC))

    def test_moscow_time_is_converted_to_utc(self):
        timestamp = parse_reminder("15.10.2099 18:30", ZoneInfo("Europe/Moscow"))
        self.assertEqual(datetime.fromtimestamp(timestamp, UTC).hour, 15)

    def test_bad_dates_past_and_dst(self):
        tz = ZoneInfo("Europe/Moscow")
        for text in ("31.02.2099 12:00", "01.01.2099 25:00", "1.1.2099 12:00", "01.01.2000 12:00", "завтра"):
            with self.subTest(text=text), self.assertRaises(InputError):
                parse_reminder(text, tz)
        for text in ("29.03.2026 02:30", "25.10.2026 02:30"):
            with self.subTest(text=text), self.assertRaises(InputError):
                parse_reminder(text, ZoneInfo("Europe/Berlin"), now=datetime(2026, 1, 1, tzinfo=UTC))

    def test_invalid_ids(self):
        for value in ("0", "-1", "abc", "1 OR 1=1", str(2**63), "9" * 10000):
            with self.subTest(value=value[:20]), self.assertRaises(InputError):
                parse_id(value)

    @patch("bot.load_dotenv")
    def test_missing_token_and_bad_timezone(self, _):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(InputError, "TELEGRAM_BOT_TOKEN"):
            Settings.load()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:fake", "TIMEZONE": "Invalid/Zone"}, clear=True):
            with self.assertRaisesRegex(InputError, "TIMEZONE"):
                Settings.load()

    @patch("bot.load_dotenv")
    def test_relative_database_is_resolved_from_lab1(self, _):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:fake"}, clear=True):
            settings = Settings.load()
        self.assertEqual(settings.database, BASE_DIR / "data/tasks.sqlite3")

    def test_logging_redacts_tokens(self):
        token = "123456:EXAMPLE_FOR_TESTS_ONLY"
        record = logging.LogRecord("test", logging.ERROR, "", 0, "https://api.telegram.org/bot%s/getMe", (token,), None)
        self.assertNotIn(token, SafeFormatter().format(record))

    def test_main_without_token_does_not_connect_or_create_database(self):
        with patch("bot.Settings.load", side_effect=InputError("Нет токена")), \
                patch("bot.configure_logging"), patch("bot.TaskStore") as store, \
                self.assertLogs("bot", level="ERROR"):
            self.assertEqual(main(), 1)
            store.assert_not_called()


class DeliveryCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.assistant = TaskAssistant(self.store, ZoneInfo("Europe/Moscow"))
        self.telegram = SimpleNamespace(send_message=AsyncMock(), set_my_commands=AsyncMock())

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def due_task(self):
        task_id = self.store.add(1, 1, "Проверить напоминание")["id"]
        self.store.set_reminder(task_id, 1, datetime.now(UTC).timestamp() - 120)
        return task_id

    async def test_overdue_sent_once_even_after_restart(self):
        task_id = self.due_task()
        await self.assistant.send_due(self.telegram)
        self.assertIn("с задержкой", self.telegram.send_message.call_args.kwargs["text"])
        self.assertEqual(self.store.get(task_id, 1)["reminder_state"], "sent")
        self.assertIsNotNone(self.store.get(task_id, 1)["sent_at"])
        self.store.close()
        self.store = TaskStore(self.path)
        restarted = TaskAssistant(self.store, ZoneInfo("Europe/Moscow"))
        await restarted.send_due(self.telegram)
        self.telegram.send_message.assert_awaited_once()

    async def test_reminder_includes_description(self):
        self.store.add(1, 1, "Купить", description="Хлеб и молоко", priority=0,
                       remind_at=datetime.now(UTC).timestamp() - 120)
        await self.assistant.send_due(self.telegram)
        text = self.telegram.send_message.call_args.kwargs["text"]
        self.assertIn("Купить", text)
        self.assertIn("Описание: Хлеб и молоко", text)
        self.assertIn("🔴 высокий", text)

    async def test_wizard_replies_with_keyboards_and_can_retry_database_error(self):
        message = SimpleNamespace(text="", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=1), effective_user=SimpleNamespace(id=1))

        async def deliver(text):
            message.text = text
            await self.assistant.handle(update, None)
            return message.reply_text.call_args

        await deliver("/add")
        args = await deliver("Собрать материалы")
        self.assertIn("Введите описание", args.args[0])
        self.assertEqual(args.kwargs["reply_markup"].keyboard[0][0].text, "Пропустить")
        args = await deliver("Ссылки и скриншоты")
        self.assertEqual(args.kwargs["reply_markup"].keyboard[0][0].text, "высокий")
        args = await deliver("нет такого приоритета")
        self.assertIsInstance(args.kwargs["reply_markup"], ReplyKeyboardMarkup)
        await deliver("средний")
        message.text = None
        await self.assistant.handle(update, None)
        self.assertEqual(self.assistant.drafts[(1, 1)].step, "reminder")
        with patch.object(self.store, "add", side_effect=sqlite3.OperationalError("locked")), self.assertLogs("bot", level="ERROR"):
            args = await deliver("Пропустить")
        self.assertIn("Не удалось", args.args[0])
        self.assertEqual(self.assistant.drafts[(1, 1)].step, "reminder")
        self.assertEqual(self.store.list_tasks(1), [])
        args = await deliver("Пропустить")
        self.assertIn("Задача добавлена", args.args[0])
        self.assertIsInstance(args.kwargs["reply_markup"], ReplyKeyboardRemove)
        self.assertEqual(len(self.store.list_tasks(1)), 1)

    async def test_future_and_cancelled_reminders_are_not_sent(self):
        for action in ("cancel", "done", "delete", "future"):
            task_id = self.due_task()
            if action == "cancel":
                self.store.set_reminder(task_id, 1, None)
            elif action == "done":
                self.store.finish(task_id, 1)
            elif action == "delete":
                self.store.delete(task_id, 1)
            else:
                self.store.set_reminder(task_id, 1, datetime.now(UTC).timestamp() + 3600)
        await self.assistant.send_due(self.telegram)
        self.telegram.send_message.assert_not_awaited()

    async def test_network_error_keeps_reminder_for_retry(self):
        task_id = self.due_task()
        self.telegram.send_message.side_effect = NetworkError("Offline")
        with self.assertLogs("bot", level="ERROR"):
            await self.assistant.send_due(self.telegram)
        row = self.store.get(task_id, 1)
        self.assertEqual(row["reminder_state"], "pending")
        self.assertIsNone(row["sent_at"])
        self.assertGreater(row["next_attempt_at"], datetime.now(UTC).timestamp())
        self.telegram.send_message.side_effect = None
        self.store.delivery_result(row, "pending", datetime.now(UTC).timestamp() - 1)
        await self.assistant.send_due(self.telegram)
        self.assertEqual(self.store.get(task_id, 1)["reminder_state"], "sent")

    @patch.dict(os.environ, {"PTB_TIMEDELTA": "true"})
    async def test_retry_after_is_respected(self):
        task_id = self.due_task()
        self.telegram.send_message.side_effect = RetryAfter(timedelta(seconds=120))
        await self.assistant.send_due(self.telegram)
        row = self.store.get(task_id, 1)
        self.assertGreater(row["next_attempt_at"], datetime.now(UTC).timestamp() + 119)
        await self.assistant.send_due(self.telegram)
        self.telegram.send_message.assert_awaited_once()

    async def test_permanent_delivery_error_is_visible_and_rearmable(self):
        for error in (Forbidden("Blocked"), BadRequest("Chat not found")):
            with self.subTest(error=type(error).__name__):
                task_id = self.due_task()
                self.telegram.send_message.side_effect = error
                with self.assertLogs("bot", level="ERROR"):
                    await self.assistant.send_due(self.telegram)
                self.assertEqual(self.store.get(task_id, 1)["reminder_state"], "failed")
                self.assertIn("не доставлено", self.assistant.respond("/list", 1, 1))
                self.store.set_reminder(task_id, 1, datetime.now(UTC).timestamp() + 3600)
                self.assertEqual(self.store.get(task_id, 1)["reminder_state"], "pending")

    async def test_concurrent_scans_do_not_duplicate_delivery(self):
        self.due_task()
        await asyncio.gather(self.assistant.send_due(self.telegram), self.assistant.send_due(self.telegram))
        self.telegram.send_message.assert_awaited_once()

    async def test_group_message_does_not_create_task(self):
        self.assistant.respond("/add", 1, 1)
        message = SimpleNamespace(text="Секрет", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="group", id=-1), effective_user=SimpleNamespace(id=1))
        await self.assistant.handle(update, None)
        self.assertEqual(self.store.list_tasks(1), [])
        self.assertIn("личный чат", message.reply_text.call_args.args[0])
        self.assertEqual(self.assistant.drafts[(1, 1)].step, "title")

    async def test_database_error_gets_friendly_response(self):
        message = SimpleNamespace(text="/list", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=1), effective_user=SimpleNamespace(id=1))
        with patch.object(self.store, "list_tasks", side_effect=sqlite3.OperationalError("locked")), self.assertLogs("bot", level="ERROR"):
            await self.assistant.handle(update, None)
        self.assertIn("Не удалось", message.reply_text.call_args.args[0])

    async def test_real_jobqueue_runs_without_telegram_connection(self):
        # Настоящий планировщик PTB, но доставка заменена локальным AsyncMock.
        task_id = self.due_task()
        settings = Settings("123456:EXAMPLE_FOR_TESTS_ONLY", ZoneInfo("Europe/Moscow"), self.path)
        app = build_application(settings, self.store)
        app.bot_data["assistant"] = self.assistant
        fake_app = SimpleNamespace(job_queue=app.job_queue, bot=self.telegram)
        await post_init(fake_app)
        jobs = app.job_queue.get_jobs_by_name("sqlite-reminders")
        self.assertEqual(len(jobs), 1)
        self.telegram.set_my_commands.assert_awaited_once()
        menu = {item.command for item in self.telegram.set_my_commands.call_args.args[0]}
        self.assertTrue({
            "list_all", "remind_off", "cancel", "search", "edit", "group_create",
            "groups", "group_tasks", "group_rename", "group_delete", "task_group",
            "task_ungroup",
        }.issubset(menu))
        # Удаляем стандартное задание; выполняем его callback реальным планировщиком
        # с подменённым контекстом, чтобы никакой запрос не ушёл в Telegram.
        jobs[0].schedule_removal()
        delivered = asyncio.Event()

        async def callback(context):
            fake_context = SimpleNamespace(application=app, bot=self.telegram)
            from bot import reminder_job
            await reminder_job(fake_context)
            delivered.set()

        app.job_queue.run_once(callback, when=0.01)
        await app.job_queue.start()
        try:
            await asyncio.wait_for(delivered.wait(), timeout=3)
        finally:
            await app.job_queue.stop()
        self.assertEqual(self.store.get(task_id, 1)["reminder_state"], "sent")

    async def test_real_dispatcher_commands_media_groups_and_edits(self):
        class LocalRequest(BaseRequest):
            """Ответы Telegram API в памяти: ни одного сетевого запроса."""

            def __init__(self):
                self.replies = []
                self.markups = []

            @property
            def read_timeout(self):
                return 5

            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def do_request(self, url, method, request_data=None, **kwargs):
                if url.endswith("/getMe"):
                    result = {"id": 123456, "is_bot": True, "first_name": "Test", "username": "test_bot"}
                elif url.endswith("/sendMessage"):
                    params = request_data.parameters
                    self.replies.append(params["text"])
                    self.markups.append(params.get("reply_markup"))
                    result = {"message_id": 10, "date": 1, "chat": {"id": params["chat_id"], "type": "private"}, "text": params["text"]}
                else:
                    raise AssertionError("Непредвиденный вызов API")
                return 200, json.dumps({"ok": True, "result": result}).encode()

        # Перехватываем создание HTTP-клиентов, сохраняя весь диспетчер PTB.
        request = LocalRequest()
        settings = Settings("123456:EXAMPLE_FOR_TESTS_ONLY", ZoneInfo("Europe/Moscow"), self.path)
        with patch("telegram.ext._applicationbuilder.HTTPXRequest", return_value=request):
            app = build_application(settings, self.store)

        async def deliver(text=None, *, user=1, chat_type="private", edited=False, photo=False):
            message = {
                "message_id": 1, "date": 1, "from": {"id": user, "is_bot": False, "first_name": "User"},
                "chat": {"id": user if chat_type == "private" else -1, "type": chat_type},
            }
            if text is not None:
                message["text"] = text
                if text.startswith("/"):
                    message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
            if photo:
                message["photo"] = [{"file_id": "test", "file_unique_id": "test", "width": 10, "height": 10}]
            update = Update.de_json({"update_id": 1, "edited_message" if edited else "message": message}, app.bot)
            await app.process_update(update)

        async with app:
            await deliver("Купить хлеб")
            await deliver("/add@ test")  # Неизвестная команда, задача не добавляется.
            await deliver("/add@other_bot Чужому боту")
            await deliver("/add Подготовить отчёт")
            await deliver("Изменённый текст", edited=True)
            self.assertEqual(len(self.store.list_tasks(1)), 2)
            await deliver("/priority 1 высокий")
            self.assertEqual(self.store.get(1, 1)["priority"], 0)
            await deliver("/list")
            self.assertIn("Купить хлеб", request.replies[-1])
            await deliver("/delete 1", user=2)
            self.assertIn("не найдена", request.replies[-1])
            await deliver(photo=True)
            self.assertIn("текстом", request.replies[-1])
            await deliver("/list", chat_type="group")
            self.assertIn("личный чат", request.replies[-1])
            await deliver("/done 1")
            self.assertEqual(len(self.store.list_tasks(1)), 1)
            await deliver("/list_all")
            self.assertIn("Купить хлеб", request.replies[-1])
            await deliver("/remind 2 15.10.2099 18:30")
            await deliver("/remind_off 2")
            self.assertEqual(self.store.get(2, 1)["reminder_state"], "none")
            await deliver("/add")
            self.assertEqual(request.replies[-1], "Введите название задачи")
            await deliver("Диалог через Telegram")
            self.assertIn("keyboard", request.markups[-1])
            await deliver("Описание через Telegram")
            await deliver("высокий")
            await deliver("Пропустить")
            self.assertEqual(self.store.get(3, 1)["description"], "Описание через Telegram")
            self.assertEqual(request.markups[-1], {"remove_keyboard": True})
            await deliver("/group_create")
            await deliver("Работа")
            await deliver("/groups")
            self.assertIn("Работа", request.replies[-1])
            await deliver("/task_group 3")
            self.assertIn("keyboard", request.markups[-1])
            await deliver("#1 · Работа")
            self.assertEqual(self.store.get(3, 1)["group_id"], 1)
            await deliver("/edit 3")
            await deliver("Изменённая задача")
            await deliver("Новое описание")
            self.assertEqual(self.store.get(3, 1)["text"], "Изменённая задача")
            self.assertEqual(self.store.get(3, 1)["description"], "Новое описание")
            self.assertEqual(request.markups[-1], {"remove_keyboard": True})
            await deliver("/search МЕНЁН")
            self.assertIn("Изменённая задача", request.replies[-1])
            await deliver("/group_tasks 1")
            self.assertIn("Изменённая задача", request.replies[-1])
            await deliver("/group_rename 1")
            await deliver("Учёба")
            self.assertEqual(self.store.get(3, 1)["group_name"], "Учёба")
            await deliver("/add")
            await deliver("Ещё одна задача")
            await deliver("Пропустить")
            await deliver("средний")
            self.assertIn("#1 · Учёба", str(request.markups[-1]))
            await deliver("#1 · Учёба")
            await deliver("Пропустить")
            self.assertEqual(self.store.get(4, 1)["group_id"], 1)
            await deliver("/task_ungroup 3")
            self.assertIsNone(self.store.get(3, 1)["group_id"])
            await deliver("/group_delete 1")
            self.assertEqual(self.store.list_groups(1), [])
            self.assertIsNone(self.store.get(4, 1)["group_id"])


if __name__ == "__main__":
    unittest.main()
