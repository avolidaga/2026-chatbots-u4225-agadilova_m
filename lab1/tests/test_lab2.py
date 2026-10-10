"""Проверки поиска, редактирования и групп во временных базах без Telegram."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardRemove

from bot import InputError, TaskAssistant, TaskStore, UTC, split_message


class Lab2Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.bot = TaskAssistant(self.store, ZoneInfo("Europe/Moscow"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def say(self, text, user=1):
        return self.bot.respond(text, user, user)

    def test_search_partial_title_description_and_cyrillic_case(self):
        product = self.store.add(1, 1, "Купить ПРОДУКТЫ")
        report = self.store.add(1, 1, "Работа", description="Подготовить отчёт", priority=0)
        self.store.finish(product["id"], 1)
        self.store.add(2, 2, "Купить продукты", description="Чужой отчёт")
        self.assertEqual([row["id"] for row in self.store.search(1, "прод")], [product["id"]])
        self.assertEqual([row["id"] for row in self.store.search(1, "ОТЧ")], [report["id"]])
        text = self.say("/search ПРОД")
        self.assertIn("выполнена", text)
        self.assertIn("Создана:", text)
        self.assertIn("Без группы", text)

    def test_search_special_characters_are_literal_and_sql_is_not_executed(self):
        self.store.add(1, 1, "100% скидка")
        self.store.add(1, 1, "file_name")
        self.store.add(1, 1, "обычная задача")
        self.assertEqual(len(self.store.search(1, "%")), 1)
        self.assertEqual(len(self.store.search(1, "_")), 1)
        self.assertEqual(self.store.search(1, "' OR 1=1 --"), [])
        with self.assertRaises(InputError):
            self.say("/search   ")
        self.assertIn("не найдены", self.say("/search неизвестное"))

    def test_search_preserves_priority_order_and_splits_long_results(self):
        low = self.store.add(1, 1, "Задача " + "x" * 2900, priority=2)
        high = self.store.add(1, 1, "Задача " + "y" * 2900, priority=0)
        self.assertEqual([row["id"] for row in self.store.search(1, "зада")], [high["id"], low["id"]])
        chunks = split_message(self.say("/search зада"))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(text.encode("utf-16-le")) // 2 <= 4000 for text in chunks))

    def test_edit_is_atomic_and_preserves_other_task_fields(self):
        group = self.store.create_group(1, "Работа")
        row = self.store.add(1, 1, "Старое", description="Описание", priority=0,
                             remind_at=datetime.now(UTC).timestamp() + 600, group_id=group["id"])
        before = dict(row)
        self.say(f"/edit {row['id']}")
        self.say("Новое название")
        self.assertEqual(dict(self.store.get(row["id"], 1)), before)
        self.say("Новое описание")
        after = dict(self.store.get(row["id"], 1))
        self.assertEqual(after.pop("text"), "Новое название")
        self.assertEqual(after.pop("description"), "Новое описание")
        before.pop("text")
        before.pop("description")
        self.assertEqual(after.pop("revision"), before.pop("revision") + 1)
        self.assertEqual(after, before)
        self.assertNotIn((1, 1), self.bot.drafts)

    def test_edit_keep_clear_invalid_and_cancel(self):
        row = self.store.add(1, 1, "Название", description="Описание")
        self.say(f"/edit {row['id']}")
        with self.assertRaises(InputError):
            self.say("   ")
        self.assertEqual(self.bot.drafts[(1, 1)].step, "edit_title")
        self.say("Другое")
        with self.assertRaises(InputError):
            self.say("😀" * 1500)
        self.assertEqual(self.bot.drafts[(1, 1)].step, "edit_description")
        self.say("/cancel")
        self.assertEqual(self.store.get(row["id"], 1)["text"], "Название")
        self.say(f"/edit {row['id']}")
        self.say("Оставить")
        self.say("Очистить")
        self.assertEqual(self.store.get(row["id"], 1)["description"], "")
        self.say(f"/edit {row['id']}")
        self.say("Оставить")
        self.say("Оставить")
        self.assertEqual(self.store.get(row["id"], 1)["text"], "Название")

    def test_group_create_rename_uniqueness_and_validation(self):
        self.assertEqual(self.say("/group_create"), "Введите название группы")
        for value in ("", "   ", "две\nстроки", "😀" * 41):
            with self.subTest(value=value[:10]), self.assertRaises(InputError):
                self.say(value)
            self.assertEqual(self.bot.drafts[(1, 1)].mode, "group_create")
        self.say("Работа")
        group = self.store.list_groups(1)[0]
        with self.assertRaisesRegex(InputError, "уже существует"):
            self.store.create_group(1, "рАБОТА")
        self.store.create_group(2, "работа")
        other = self.store.create_group(1, "Дом")
        self.say(f"/group_rename {group['id']}")
        with self.assertRaisesRegex(InputError, "уже существует"):
            self.say("дом")
        self.say("Учёба")
        self.assertEqual(self.store.get_group(group["id"], 1)["name"], "Учёба")
        self.assertEqual(self.store.get_group(other["id"], 1)["name"], "Дом")
        self.assertIn("Учёба", self.say("/groups"))

    def test_group_assign_move_ungroup_and_active_counts(self):
        first = self.store.create_group(1, "Дом")
        second = self.store.create_group(1, "Работа")
        task = self.store.add(1, 1, "Позвонить")
        self.say(f"/task_group {task['id']}")
        self.say(f"#{first['id']} · Дом")
        self.assertEqual(self.store.get(task["id"], 1)["group_name"], "Дом")
        self.say(f"/task_group {task['id']}")
        self.say(str(second["id"]))
        self.assertEqual(self.store.list_groups(1)[0]["active_count"], 0)
        self.assertEqual(self.store.list_groups(1)[1]["active_count"], 1)
        self.store.finish(task["id"], 1)
        self.assertEqual(self.store.list_groups(1)[1]["active_count"], 0)
        self.assertIn("Позвонить", self.say(f"/group_tasks {second['id']}"))
        self.assertIn("выполнена", self.say(f"/group_tasks {second['id']}"))
        self.assertNotIn("Позвонить", self.say(f"/group_tasks {first['id']}"))
        self.say(f"/task_ungroup {task['id']}")
        self.assertIsNone(self.store.get(task["id"], 1)["group_id"])

    def test_group_delete_keeps_tasks_and_reminders(self):
        group = self.store.create_group(1, "Дом")
        pending = self.store.add(1, 1, "Купить", description="Хлеб", priority=2,
                                 remind_at=datetime.now(UTC).timestamp() + 600, group_id=group["id"])
        completed = self.store.add(1, 1, "Готово", group_id=group["id"])
        self.store.finish(completed["id"], 1)
        before = dict(pending)
        self.say(f"/group_delete {group['id']}")
        self.assertEqual(len(self.store.list_tasks(1, True)), 2)
        self.assertEqual(self.store.get(completed["id"], 1)["done"], 1)
        after = dict(self.store.get(pending["id"], 1))
        for key in ("group_id", "group_name"):
            before.pop(key)
            self.assertIsNone(after.pop(key))
        self.assertEqual(after.pop("revision"), before.pop("revision") + 1)
        self.assertEqual(before, after)

    def test_failed_group_delete_rolls_back_task_links(self):
        group = self.store.create_group(1, "Работа")
        task = self.store.add(1, 1, "Отчёт", group_id=group["id"])
        self.store.connection.executescript("""
            CREATE TRIGGER reject_delete BEFORE DELETE ON task_groups
            BEGIN SELECT RAISE(ABORT, 'test failure'); END;
        """)
        with self.assertRaises(sqlite3.Error):
            self.store.delete_group(group["id"], 1)
        self.assertEqual(self.store.get(task["id"], 1)["group_id"], group["id"])
        self.assertEqual(len(self.store.list_groups(1)), 1)

    def test_owners_checked_for_tasks_groups_and_forged_buttons(self):
        group = self.store.create_group(1, "Личная")
        task = self.store.add(1, 1, "Личная задача", group_id=group["id"])
        own_group = self.store.create_group(2, "Другая")
        own_task = self.store.add(2, 2, "Своя задача")
        for command in (
            f"/edit {task['id']}", f"/group_tasks {group['id']}", f"/group_rename {group['id']}",
            f"/group_delete {group['id']}", f"/task_group {task['id']}", f"/task_ungroup {task['id']}",
        ):
            with self.subTest(command=command), self.assertRaises(InputError):
                self.say(command, user=2)
        self.say(f"/task_group {own_task['id']}", user=2)
        with self.assertRaisesRegex(InputError, "не найдена"):
            self.say(f"#{group['id']} · Личная", user=2)
        self.assertIsNone(self.store.get(own_task["id"], 2)["group_id"])
        self.assertEqual(self.store.get(task["id"], 1)["group_id"], group["id"])
        with self.assertRaises(InputError):
            self.store.add(2, 2, "Подделка", group_id=group["id"])
        with self.assertRaises(InputError):
            self.store.assign_group(task["id"], 1, own_group["id"])
        self.assertNotIn("Личная", self.say("/groups", user=2))
        self.assertIn("не найдены", self.say("/search Личная", user=2))

    def test_add_wizard_with_group_and_skip(self):
        group = self.store.create_group(1, "Учёба")
        self.say("/add")
        self.say("Отчёт")
        self.say("Описание")
        self.assertIn("Выберите группу", self.say("высокий"))
        self.assertEqual(self.store.list_tasks(1), [])
        self.assertIn("Установить напоминание", self.say(f"#{group['id']} · Учёба"))
        self.say("Пропустить")
        self.assertEqual(self.store.list_tasks(1)[0]["group_id"], group["id"])
        self.say("/add")
        self.say("Без группы")
        self.say("Пропустить")
        self.say("низкий")
        self.say("Пропустить")
        self.say("Пропустить")
        self.assertIsNone(self.store.list_tasks(1)[1]["group_id"])
        self.say("/add Быстрая")
        self.assertIsNone(self.store.list_tasks(1)[1]["group_id"])

    def test_deleted_group_during_add_can_be_reselected(self):
        group = self.store.create_group(1, "Работа")
        for text in ("/add", "Отчёт", "Пропустить", "средний", str(group["id"])):
            self.say(text)
        self.store.delete_group(group["id"], 1)
        with self.assertRaisesRegex(InputError, "удалена"):
            self.say("Пропустить")
        self.assertEqual(self.bot.drafts[(1, 1)].step, "add_group")
        self.say("Пропустить")
        self.say("Пропустить")
        self.assertEqual(len(self.store.list_tasks(1)), 1)
        self.assertIsNone(self.store.list_tasks(1)[0]["group_id"])

    def test_dialog_cancellation_read_commands_and_users(self):
        task = self.store.add(1, 1, "Задача")
        self.say(f"/edit {task['id']}")
        for text in ("/groups", "/list", "/search задача", "/help"):
            self.say(text)
            self.assertEqual(self.bot.drafts[(1, 1)].mode, "edit")
        self.say("/group_create", user=2)
        self.say("Работа", user=2)
        self.assertEqual(self.bot.drafts[(1, 1)].mode, "edit")
        self.say("/group_create")
        self.say("/cancel")
        self.assertEqual(self.store.list_groups(1), [])
        self.say("/group_create")
        self.say("/start")
        self.assertNotIn((1, 1), self.bot.drafts)
        self.assertEqual(self.store.get(task["id"], 1)["text"], "Задача")

    def test_invalid_parameters_empty_groups_and_unknown_ids(self):
        for text in (
            "/edit", "/edit 0", "/groups x", "/group_create x", "/group_tasks",
            "/group_rename 1 2", "/group_delete abc", "/task_group", "/task_ungroup 1 2",
        ):
            with self.subTest(text=text), self.assertRaises(InputError):
                self.say(text)
        self.assertIn("Групп пока нет", self.say("/groups"))
        task = self.store.add(1, 1, "Задача")
        self.assertIn("Сначала создайте", self.say(f"/task_group {task['id']}"))
        for text in ("/edit 999", "/group_tasks 999", "/group_delete 999", "/task_ungroup 999"):
            with self.subTest(text=text), self.assertRaises(InputError):
                self.say(text)

    def test_restart_preserves_groups_edits_and_assignments(self):
        group = self.store.create_group(1, "Работа")
        task = self.store.add(1, 1, "Название", group_id=group["id"])
        self.store.update_text(task["id"], 1, "Новое", "Подробности")
        self.store.close()
        self.store = TaskStore(self.path)
        row = self.store.get(task["id"], 1)
        self.assertEqual(row["text"], "Новое")
        self.assertEqual(row["description"], "Подробности")
        self.assertEqual(row["group_name"], "Работа")
        self.assertEqual(self.store.list_groups(1)[0]["active_count"], 1)

    def test_migration_from_lab1_without_group_column_preserves_all_values(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript("""
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER,
                    text TEXT, description TEXT, priority INTEGER, done INTEGER, created_at REAL,
                    remind_at REAL, reminder_state TEXT, next_attempt_at REAL, sent_at REAL
                );
                INSERT INTO tasks VALUES (7, 1, 1, 'Старая', 'Описание', 0, 0,
                    100, 200, 'pending', 200, NULL);
            """)
            connection.row_factory = sqlite3.Row
            original = dict(connection.execute("SELECT * FROM tasks").fetchone())
        migrated = TaskStore(path)
        try:
            row = dict(migrated.get(7, 1))
            self.assertIsNone(row.pop("group_id"))
            self.assertIsNone(row.pop("group_name"))
            self.assertIsNone(row.pop("deadline_at"))
            self.assertEqual(row.pop("deadline_has_time"), 1)
            self.assertEqual(row.pop("revision"), 0)
            self.assertEqual(row, original)
            group = migrated.create_group(1, "Миграция")
            migrated.assign_group(7, 1, group["id"])
        finally:
            migrated.close()
        migrated = TaskStore(path)
        try:
            self.assertEqual(migrated.get(7, 1)["group_name"], "Миграция")
            self.assertEqual(migrated.get(7, 1)["reminder_state"], "pending")
            names = {row[0] for row in migrated.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertNotIn("messages", names)
            self.assertNotIn("users", names)
        finally:
            migrated.close()


class Lab2DeliveryCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")
        self.bot = TaskAssistant(self.store, ZoneInfo("Europe/Moscow"))

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_delivery_uses_edited_task_and_renamed_group(self):
        group = self.store.create_group(1, "Старая группа")
        task = self.store.add(1, 1, "Старое название", description="Старое описание",
                             remind_at=datetime.now(UTC).timestamp() - 60, group_id=group["id"])
        self.store.update_text(task["id"], 1, "Новое название", "Новое описание")
        self.store.rename_group(group["id"], 1, "Новая группа")
        telegram = SimpleNamespace(send_message=AsyncMock())
        await self.bot.send_due(telegram)
        text = telegram.send_message.call_args.kwargs["text"]
        for value in ("Новое название", "Новое описание", "Новая группа"):
            self.assertIn(value, text)
        self.assertNotIn("Старое", text)
        self.assertEqual(self.store.get(task["id"], 1)["reminder_state"], "sent")

    async def test_database_edit_error_retains_draft_and_removes_keyboard_after_retry(self):
        task = self.store.add(1, 1, "Старое")
        message = SimpleNamespace(text="", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=1), effective_user=SimpleNamespace(id=1))

        async def deliver(text):
            message.text = text
            await self.bot.handle(update, None)

        await deliver(f"/edit {task['id']}")
        await deliver("Новое")
        with patch.object(self.store, "update_text", side_effect=sqlite3.OperationalError("locked")), self.assertLogs("bot", level="ERROR"):
            await deliver("Описание")
        self.assertEqual(self.bot.drafts[(1, 1)].step, "edit_description")
        self.assertEqual(self.store.get(task["id"], 1)["text"], "Старое")
        await deliver("Описание")
        self.assertEqual(self.store.get(task["id"], 1)["text"], "Новое")
        self.assertIsInstance(message.reply_text.call_args.kwargs["reply_markup"], ReplyKeyboardRemove)

    async def test_group_keyboard_error_is_reported_without_losing_dialog(self):
        group = self.store.create_group(1, "Работа")
        task = self.store.add(1, 1, "Задача")
        self.bot.respond(f"/task_group {task['id']}", 1, 1)
        message = SimpleNamespace(text="/groups", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type="private", id=1), effective_user=SimpleNamespace(id=1))
        with patch.object(self.store, "list_groups", side_effect=sqlite3.OperationalError("locked")), self.assertLogs("bot", level="ERROR"):
            await self.bot.handle(update, None)
        self.assertIn("Не удалось", message.reply_text.call_args.args[0])
        self.assertEqual(self.bot.drafts[(1, 1)].mode, "assign_group")
        self.bot.respond(str(group["id"]), 1, 1)
        self.assertEqual(self.store.get(task["id"], 1)["group_id"], group["id"])


if __name__ == "__main__":
    unittest.main()
