"""Новая версия: кнопки, сроки, подтверждения, ошибки. Только временные базы и fake API."""

import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram.error import BadRequest, NetworkError

from bot import InputError, Settings, TaskAssistant, TaskStore, UTC, main
from buttons import ButtonAssistant, Draft, View
from task_dates import deadline_day, parse_deadline, quick_reminder

MOSCOW = ZoneInfo("Europe/Moscow")


def token(screen, label):
    return next(button.callback_data for row in screen.markup.inline_keyboard for button in row
                if label in button.text)


class DatesCase(unittest.TestCase):
    def test_date_only_boundary_and_display_day(self):
        when, has_time = parse_deadline("10.10.2026", MOSCOW)
        self.assertFalse(has_time)
        self.assertEqual(datetime.fromtimestamp(when, UTC), datetime(2026, 10, 10, 21, tzinfo=UTC))
        self.assertEqual(deadline_day(when, has_time, MOSCOW).isoformat(), "2026-10-10")

    def test_timed_deadline_can_be_in_past(self):
        when, has_time = parse_deadline("10.10.2020 14:30", MOSCOW)
        self.assertTrue(has_time)
        self.assertEqual(datetime.fromtimestamp(when, UTC).hour, 11)
        self.assertEqual(deadline_day(when, has_time, MOSCOW).isoformat(), "2020-10-10")

    def test_short_date_uses_local_year_without_rollover(self):
        now = datetime(2026, 12, 31, 22, tzinfo=UTC)
        when, _ = parse_deadline("01.01", MOSCOW, now)
        self.assertEqual(deadline_day(when, False, MOSCOW).year, 2027)
        when, _ = parse_deadline("01.01", MOSCOW, datetime(2026, 12, 30, tzinfo=UTC))
        self.assertEqual(deadline_day(when, False, MOSCOW).year, 2026)

    def test_calendar_day_across_dst_is_not_24_hours(self):
        berlin = ZoneInfo("Europe/Berlin")
        start, _ = parse_deadline("28.03.2026", berlin)
        end, _ = parse_deadline("29.03.2026", berlin)
        self.assertEqual(end - start, 23 * 3600)

    def test_invalid_dates_and_dst_are_rejected(self):
        for value in ("31.02.2026", "29.02.2025", "1.1", "10.10 25:00", "10.10 text"):
            with self.subTest(value=value):
                with self.assertRaises(InputError):
                    parse_deadline(value, MOSCOW)
        with self.assertRaises(InputError):
            parse_deadline("31.12.9999", MOSCOW)
        for value in ("29.03.2026 02:30", "25.10.2026 02:30"):
            with self.assertRaises(InputError):
                parse_deadline(value, ZoneInfo("Europe/Berlin"))

    def test_quick_relative_reminders(self):
        now = datetime(2026, 10, 10, 12, tzinfo=UTC)
        self.assertEqual(quick_reminder("15", MOSCOW, now=now), now.timestamp() + 900)
        self.assertEqual(quick_reminder("60", MOSCOW, now=now), now.timestamp() + 3600)

    def test_tomorrow_uses_calendar_and_crosses_new_year(self):
        now = datetime(2026, 12, 31, 20, tzinfo=UTC)
        when = quick_reminder("tomorrow", MOSCOW, now=now)
        self.assertEqual(datetime.fromtimestamp(when, MOSCOW).isoformat(), "2027-01-01T09:00:00+03:00")

    def test_before_deadline_only_if_in_future(self):
        now = datetime(2026, 10, 10, 12, tzinfo=UTC)
        self.assertEqual(quick_reminder("before", MOSCOW, now.timestamp() + 7200, now), now.timestamp() + 3600)
        for deadline in (None, now.timestamp() + 3600, now.timestamp() - 1):
            with self.assertRaises(InputError):
                quick_reminder("before", MOSCOW, deadline, now)


class ButtonCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.ui = ButtonAssistant(TaskAssistant(self.store, MOSCOW))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def say(self, text, user=1):
        return self.ui.respond(text, user, user)

    def click(self, screen, label, user=1):
        return self.ui.press(token(screen, label), user, user)

    def create_review(self, title="Название", description="Описание", group=None):
        screen = self.click(self.say("/start"), "Добавить задачу")
        self.assertIn("название", screen.text)
        self.say(title)
        screen = self.say(description)
        screen = self.click(screen, "высокий")
        screen = self.click(screen, group or "Пропустить")
        screen = self.click(screen, "Пропустить")
        return self.click(screen, "Пропустить")

    def test_plain_text_does_not_add_and_explicit_fast_add_works(self):
        for text in ("Случайный текст", "Пропустить", "Меню", "высокий", ""):
            self.say(text)
        self.assertEqual(self.store.list_tasks(1), [])
        screen = self.say("/add Явная задача")
        self.assertIn("Явная задача", screen.text)
        self.assertEqual(len(self.store.list_tasks(1)), 1)

    def test_entire_add_flow_atomic_and_duplicate_save_rejected(self):
        review = self.create_review()
        self.assertEqual(self.store.list_tasks(1), [])
        saved = token(review, "Сохранить")
        screen = self.ui.press(saved, 1, 1)
        self.assertIn("Задача добавлена", screen.text)
        row = self.store.list_tasks(1)[0]
        self.assertEqual((row["text"], row["description"], row["priority"]), ("Название", "Описание", 0))
        with self.assertRaises(InputError):
            self.ui.press(saved, 1, 1)
        self.say("Ещё текст")
        self.assertEqual(len(self.store.list_tasks(1)), 1)

    def test_reserved_words_are_literal_input(self):
        review = self.create_review("Меню", "Пропустить")
        self.click(review, "Сохранить")
        row = self.store.list_tasks(1)[0]
        self.assertEqual(row["text"], "Меню")
        self.assertEqual(row["description"], "Пропустить")

    def test_invalid_input_keeps_draft_and_cancel_never_writes(self):
        screen = self.say("/add")
        with self.assertRaises(InputError):
            self.say("😀" * 1501)
        self.assertEqual(self.ui.session(1, 1).draft.step, "title")
        screen = self.say("Правильное название")
        self.click(screen, "Отмена")
        self.assertEqual(self.store.list_tasks(1), [])

    def test_back_preserves_fields_but_does_not_revive_old_confirmation(self):
        review = self.create_review()
        old_save = token(review, "Сохранить")
        back = self.click(review, "Назад")
        self.assertEqual(self.ui.session(1, 1).draft.title, "Название")
        review = self.click(back, "Пропустить")
        with self.assertRaises(InputError):
            self.ui.press(old_save, 1, 1)
        self.click(review, "Сохранить")

    def test_new_form_replaces_old_and_users_are_independent(self):
        first = self.say("/add")
        cancel = token(first, "Отмена")
        second = self.say("/group_create")
        self.assertIn("заменён", second.text)
        with self.assertRaises(InputError):
            self.ui.press(cancel, 1, 1)
        self.say("/add", user=2)
        self.say("Другая задача", user=2)
        self.assertEqual(self.ui.session(1, 1).draft.step, "group_name")
        self.assertEqual(self.ui.session(2, 2).draft.step, "description")

    def test_read_views_do_not_commit_draft(self):
        self.say("/add")
        self.say("Черновик")
        self.say("/list")
        self.say("/groups")
        self.say("/help")
        self.assertEqual(self.ui.session(1, 1).draft.title, "Черновик")
        self.assertEqual(self.store.list_tasks(1), [])

    def test_delete_command_cancel_confirm_and_double_click(self):
        row = self.store.add(1, 1, "Удаляемая", remind_at=datetime.now(UTC).timestamp() + 600)
        screen = self.say(f"/delete {row['id']}")
        self.assertEqual(self.store.get(row["id"], 1)["reminder_state"], "pending")
        old = token(screen, "Да, удалить")
        self.click(screen, "Отмена")
        with self.assertRaises(InputError):
            self.ui.press(old, 1, 1)
        screen = self.say(f"/delete {row['id']}")
        confirm = token(screen, "Да, удалить")
        self.ui.press(confirm, 1, 1)
        self.assertEqual(self.store.list_tasks(1, True), [])
        with self.assertRaises(InputError):
            self.ui.press(confirm, 1, 1)

    def test_expired_and_restarted_confirmations_cannot_delete(self):
        row = self.store.add(1, 1, "Сохранить")
        screen = self.say(f"/delete {row['id']}")
        key = token(screen, "Да, удалить")
        self.ui.actions[key] = replace(self.ui.actions[key], expires=0)
        with self.assertRaises(InputError):
            self.ui.press(key, 1, 1)
        restarted = ButtonAssistant(TaskAssistant(self.store, MOSCOW))
        with self.assertRaises(InputError):
            restarted.press(key, 1, 1)
        self.assertEqual(self.store.get(row["id"], 1)["text"], "Сохранить")

    def test_stale_card_and_edit_draft_cannot_overwrite_task(self):
        row = self.store.add(1, 1, "Старое")
        card = self.ui.card(row["id"], 1, 1)
        done = token(card, "Выполнить")
        self.store.update_text(row["id"], 1, "Новое", "")
        with self.assertRaises(InputError):
            self.ui.press(done, 1, 1)
        self.say(f"/edit {row['id']}")
        self.say("Мой черновик")
        screen = self.say("Описание")
        self.store.set_priority(row["id"], 1, 2)
        with self.assertRaises(InputError):
            self.click(screen, "Сохранить")
        self.assertEqual(self.store.get(row["id"], 1)["text"], "Новое")

    def test_foreign_and_forged_callback_data_are_rejected(self):
        row = self.store.add(1, 1, "Секретная")
        key = token(self.ui.card(row["id"], 1, 1), "Выполнить")
        for data, user, chat in ((key, 2, 2), (key, 1, 2), ("b2:made-up", 1, 1), ("done:1 OR 1=1", 1, 1), (None, 1, 1)):
            with self.assertRaises(InputError):
                self.ui.press(data, user, chat)
        self.assertEqual(self.store.get(row["id"], 1)["done"], 0)
        for command in ("/edit", "/delete", "/task_group", "/done", "/priority"):
            with self.assertRaises(InputError):
                self.say(f"{command} {row['id']}" + (" высокий" if command == "/priority" else ""), user=2)

    def test_task_edit_priority_group_and_deadline_buttons(self):
        group = self.store.create_group(1, "Работа")
        row = self.store.add(1, 1, "Исходное", "Текст", remind_at=datetime.now(UTC).timestamp() + 600)
        task_id = row["id"]
        screen = self.click(self.ui.card(task_id, 1, 1), "Изменить")
        screen = self.click(screen, "Оставить")
        screen = self.click(screen, "Очистить")
        self.click(screen, "Сохранить")
        self.assertEqual(self.store.get(task_id, 1)["description"], "")
        screen = self.click(self.ui.card(task_id, 1, 1), "Приоритет")
        screen = self.click(screen, "низкий")
        self.click(screen, "Сохранить")
        self.assertEqual(self.store.get(task_id, 1)["priority"], 2)
        screen = self.click(self.ui.card(task_id, 1, 1), "Группа")
        screen = self.click(screen, "Работа")
        self.click(screen, "Сохранить")
        self.assertEqual(self.store.get(task_id, 1)["group_id"], group["id"])
        screen = self.click(self.ui.card(task_id, 1, 1), "Срок")
        self.click(screen, "Дата и время")
        screen = self.say("15.10.2099 18:30")
        self.assertIsNotNone(self.store.get(task_id, 1)["remind_at"])
        self.click(screen, "Сохранить")
        after = self.store.get(task_id, 1)
        self.assertEqual(after["remind_at"], row["remind_at"])
        self.assertEqual(after["deadline_has_time"], 1)
        screen = self.click(self.ui.card(task_id, 1, 1), "Срок")
        screen = self.click(screen, "Убрать срок")
        self.click(screen, "Сохранить")
        self.assertIsNone(self.store.get(task_id, 1)["deadline_at"])
        self.assertEqual(self.store.get(task_id, 1)["remind_at"], row["remind_at"])

    def test_reminder_choices_and_cancellation_do_not_change_deadline(self):
        deadline, _ = parse_deadline("15.10.2099", MOSCOW)
        row = self.store.add(1, 1, "Срок", deadline_at=deadline, deadline_has_time=False)
        self.assertIsNone(row["remind_at"])
        screen = self.click(self.ui.card(row["id"], 1, 1), "Напоминание")
        screen = self.click(screen, "За час до срока")
        self.assertIsNone(self.store.get(row["id"], 1)["remind_at"])
        self.click(screen, "Сохранить")
        self.assertEqual(self.store.get(row["id"], 1)["remind_at"], deadline - 3600)
        screen = self.click(self.ui.card(row["id"], 1, 1), "Напоминание")
        screen = self.click(screen, "Через 15 минут")
        self.click(screen, "Сохранить")
        self.assertLess(self.store.get(row["id"], 1)["remind_at"], deadline - 3600)
        screen = self.click(self.ui.card(row["id"], 1, 1), "Напоминание")
        screen = self.click(screen, "Отключить")
        self.click(screen, "Сохранить")
        after = self.store.get(row["id"], 1)
        self.assertIsNone(after["remind_at"])
        self.assertEqual(after["deadline_at"], deadline)

    def test_expired_time_during_add_can_be_corrected(self):
        review = self.create_review()
        self.ui.session(1, 1).draft.remind_at = datetime.now(UTC).timestamp() - 1
        with self.assertRaises(InputError):
            self.click(review, "Сохранить")
        self.assertEqual(self.store.list_tasks(1), [])
        self.assertEqual(self.ui.session(1, 1).draft.step, "reminder")
        screen = self.ui.form(1, 1)
        screen = self.click(screen, "Пропустить")
        self.click(screen, "Сохранить")

    def test_finished_task_keeps_deadline_and_has_no_reminder_action(self):
        when, _ = parse_deadline("10.10.2020", MOSCOW)
        row = self.store.add(1, 1, "Выполнить", deadline_at=when, remind_at=datetime.now(UTC).timestamp() + 60)
        card = self.click(self.ui.card(row["id"], 1, 1), "Выполнить")
        after = self.store.get(row["id"], 1)
        self.assertEqual(after["deadline_at"], when)
        self.assertIsNone(after["remind_at"])
        captions = [button.text for line in card.markup.inline_keyboard for button in line]
        self.assertNotIn("⏰ Напоминание", captions)

    def test_groups_created_renamed_assigned_and_deleted_with_buttons(self):
        screen = self.click(self.say("/groups"), "Создать группу")
        screen = self.say("Группа")
        card = self.click(screen, "Сохранить")
        group_id = self.store.list_groups(1)[0]["id"]
        self.store.add(1, 1, "Задача", remind_at=datetime.now(UTC).timestamp() + 300)
        screen = self.click(card, "Добавить существующую")
        self.click(screen, "Задача")
        row = self.store.list_tasks(1)[0]
        self.assertEqual(row["group_id"], group_id)
        card = self.ui.group_card(group_id, 1, 1)
        self.click(card, "Переименовать")
        screen = self.say("Новая группа")
        card = self.click(screen, "Сохранить")
        screen = self.click(card, "Удалить группу")
        self.assertEqual(len(self.store.list_groups(1)), 1)
        self.click(screen, "Да, удалить")
        after = self.store.get(row["id"], 1)
        self.assertIsNone(after["group_id"])
        self.assertEqual(after["remind_at"], row["remind_at"])

    def test_group_delete_confirmation_invalidated_by_new_member(self):
        group = self.store.create_group(1, "Группа")
        screen = self.say(f"/group_delete {group['id']}")
        self.store.add(1, 1, "Поздняя задача", group_id=group["id"])
        with self.assertRaises(InputError):
            self.click(screen, "Да, удалить")
        self.assertEqual(len(self.store.list_groups(1)), 1)

    def test_pagination_search_context_and_clamping_after_delete(self):
        for index in range(7):
            self.store.add(1, 1, f"Задача {index}")
        screen = self.say("/search дача")
        screen = self.click(screen, "Следующая")
        self.assertIn("2/2", screen.text)
        card = self.click(screen, "Задача 6")
        screen = self.click(card, "Удалить")
        screen = self.click(screen, "Да, удалить")
        self.assertIn("Поиск: дача", screen.text)
        self.assertIn("2/2", screen.text)
        card = self.click(screen, "Задача 5")
        screen = self.click(card, "Удалить")
        screen = self.click(screen, "Да, удалить")
        self.assertIn("1/1", screen.text)

    def test_all_groups_accessible_without_id_and_payload_is_bounded(self):
        for index in range(12):
            self.store.create_group(1, f"Группа {index}")
        screen = self.say("/groups")
        screen = self.click(screen, "Следующая")
        screen = self.click(screen, "Следующая")
        screen = self.click(screen, "Группа 11")
        screen = self.click(screen, "К группам")
        self.assertIn("3/3", screen.text)
        self.say("/add")
        self.say("Название")
        screen = self.say("Описание")
        screen = self.click(screen, "средний")
        screen = self.click(screen, "Следующая")
        screen = self.click(screen, "Следующая")
        screen = self.click(screen, "Группа 11")
        self.assertEqual(self.ui.session(1, 1).draft.group_id, 12)
        for row in screen.markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)
                self.assertNotIn("Название", button.callback_data)

    def test_sqlite_failure_does_not_lose_draft_or_create_duplicate(self):
        review = self.create_review()
        with patch.object(self.store, "add", side_effect=sqlite3.OperationalError("temporary")):
            with self.assertRaises(sqlite3.Error):
                self.click(review, "Сохранить")
        self.assertEqual(self.ui.session(1, 1).draft.title, "Название")
        self.click(self.ui.form(1, 1), "Сохранить")
        self.assertEqual(len(self.store.list_tasks(1)), 1)

    def test_render_failure_after_commit_cannot_repeat_add(self):
        review = self.create_review()
        key = token(review, "Сохранить")
        with patch.object(self.ui, "card", side_effect=sqlite3.OperationalError("read failed")):
            with self.assertRaises(sqlite3.Error):
                self.ui.press(key, 1, 1)
        with self.assertRaises(InputError):
            self.ui.press(key, 1, 1)
        self.assertIsNone(self.ui.session(1, 1).draft)
        self.assertEqual(len(self.store.list_tasks(1)), 1)

    def test_read_failure_inside_add_rolls_back_insert_and_preserves_draft(self):
        group = self.store.create_group(1, "Работа")
        review = self.create_review(group="Работа")
        with patch.object(self.store, "get", side_effect=sqlite3.OperationalError("read failed")):
            with self.assertRaises(sqlite3.Error):
                self.click(review, "Сохранить")
        self.assertEqual(self.store.list_tasks(1), [])
        self.assertEqual(self.store.get_group(group["id"], 1)["revision"], group["revision"])
        self.assertEqual(self.ui.session(1, 1).draft.title, "Название")
        self.click(review, "Сохранить")
        self.assertEqual(len(self.store.list_tasks(1)), 1)

    def test_read_failure_inside_group_creation_rolls_back_insert(self):
        self.say("/group_create")
        review = self.say("Группа")
        with patch.object(self.store, "get_group", side_effect=sqlite3.OperationalError("read failed")):
            with self.assertRaises(sqlite3.Error):
                self.click(review, "Сохранить")
        self.assertEqual(self.store.list_groups(1), [])
        self.assertEqual(self.ui.session(1, 1).draft.title, "Группа")
        self.click(review, "Сохранить")
        self.assertEqual(len(self.store.list_groups(1)), 1)

    def test_deleted_group_in_add_can_be_reselected_without_losing_title(self):
        group = self.store.create_group(1, "Удаляемая")
        review = self.create_review(group="Удаляемая")
        self.store.delete_group(group["id"], 1)
        review = self.ui.form(1, 1)
        self.assertIn("группа удалена", review.text)
        with self.assertRaises(InputError):
            self.click(review, "Сохранить")
        draft = self.ui.session(1, 1).draft
        self.assertEqual((draft.step, draft.title), ("group", "Название"))
        screen = self.click(self.ui.form(1, 1), "Пропустить")
        screen = self.click(screen, "Пропустить")
        screen = self.click(screen, "Пропустить")
        self.click(screen, "Сохранить")
        self.assertIsNone(self.store.list_tasks(1)[0]["group_id"])

    def test_add_from_group_can_keep_preselected_group_without_id(self):
        group = self.store.create_group(1, "Учёба")
        screen = self.ui.group_card(group["id"], 1, 1)
        screen = self.click(screen, "Добавить новую задачу")
        self.say("Название")
        screen = self.say("Описание")
        screen = self.click(screen, "средний")
        self.assertIn("Выбрана: Учёба", screen.text)
        screen = self.click(screen, "Оставить выбранную группу")
        screen = self.click(screen, "Пропустить")
        screen = self.click(screen, "Пропустить")
        self.click(screen, "Сохранить")
        self.assertEqual(self.store.list_tasks(1)[0]["group_id"], group["id"])

    def test_search_result_add_button_is_current(self):
        screen = self.click(self.say("/start"), "Поиск")
        screen = self.say("ничего")
        screen = self.click(screen, "Добавить задачу")
        self.assertIn("название", screen.text)

    def test_filters_are_independent_of_reminders_and_owner_checked(self):
        now = datetime(2026, 10, 10, 12, tzinfo=UTC)
        today, _ = parse_deadline("10.10.2026", MOSCOW)
        old, _ = parse_deadline("09.10.2026", MOSCOW)
        timed = self.store.add(1, 1, "Ранее сегодня", priority=0, deadline_at=now.timestamp() - 60)
        all_day = self.store.add(1, 1, "Весь день", deadline_at=today, deadline_has_time=False)
        overdue = self.store.add(1, 1, "Вчера", priority=2, deadline_at=old, deadline_has_time=False)
        reminder = self.store.add(1, 1, "Только уведомление", remind_at=now.timestamp() + 60)
        done = self.store.add(1, 1, "Выполнена", deadline_at=today, deadline_has_time=False)
        self.store.finish(done["id"], 1)
        self.store.add(2, 2, "Чужая", deadline_at=today, deadline_has_time=False)
        def ids(view, at=now):
            return [row["id"] for row in self.store.filtered_tasks(1, view, MOSCOW, now=at)]
        self.assertEqual(ids("today"), [timed["id"], all_day["id"]])
        self.assertEqual(ids("overdue"), [timed["id"], overdue["id"]])
        self.assertEqual(ids("reminders"), [reminder["id"]])
        self.assertEqual(ids("high"), [timed["id"]])
        self.assertEqual(ids("done"), [done["id"]])
        boundary = datetime.fromtimestamp(today, UTC)
        self.assertIn(all_day["id"], ids("overdue", boundary))
        self.assertEqual(ids("today", boundary), [])
        with self.assertRaises(InputError):
            self.store.filtered_tasks(1, "unknown", MOSCOW)

    def test_group_filter_search_and_ownership(self):
        group = self.store.create_group(1, "Личная")
        other = self.store.create_group(2, "Чужая")
        task = self.store.add(1, 1, "Отчёт", group_id=group["id"], priority=0)
        self.store.add(1, 1, "Другой отчёт")
        screen = self.ui.listing(1, 1, View("high", group_id=group["id"], query="отч"))
        self.assertIn("задач: 1", screen.text)
        self.assertIn(f"#{task['id']}", screen.text)
        with self.assertRaises(InputError):
            self.ui.listing(1, 1, View(group_id=other["id"]))

    def test_restart_keeps_deadlines_and_migration_is_idempotent(self):
        when, flag = parse_deadline("15.10.2099", MOSCOW)
        row = self.store.add(1, 1, "Срок", deadline_at=when, deadline_has_time=flag)
        before = dict(row)
        self.store.close()
        self.store = TaskStore(self.path)
        self.assertEqual(dict(self.store.get(row["id"], 1)), before)
        self.ui = ButtonAssistant(TaskAssistant(self.store, MOSCOW))
        self.assertIn("до конца дня", self.ui.card(row["id"], 1, 1).text)

    def test_completed_task_is_not_marked_overdue_when_editing(self):
        row = self.store.add(1, 1, "Выполнена", deadline_at=1)
        self.store.finish(row["id"], 1)
        screen = self.say(f"/edit {row['id']}")
        screen = self.click(screen, "Оставить")
        screen = self.click(screen, "Оставить")
        self.assertNotIn("ПРОСРОЧЕНА", screen.text)
        self.click(screen, "Сохранить")
        self.assertTrue(self.store.get(row["id"], 1)["done"])


class ButtonDeliveryCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")
        self.ui = ButtonAssistant(TaskAssistant(self.store, MOSCOW))
        self.bot = SimpleNamespace(send_message=AsyncMock(), username="test_bot")

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def update(self, data, *, user=1, private=True):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user),
            effective_chat=SimpleNamespace(id=user, type="private" if private else "group"),
            callback_query=SimpleNamespace(data=data, answer=AsyncMock(),
                                           message=SimpleNamespace(edit_text=AsyncMock())),
        )

    async def test_callback_answers_and_falls_back_when_message_unavailable(self):
        screen = self.ui.home(1, 1)
        update = self.update(token(screen, "Мои задачи"))
        update.callback_query.message.edit_text.side_effect = BadRequest("message to edit not found")
        await self.ui.handle_callback(update, SimpleNamespace(bot=self.bot))
        update.callback_query.answer.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_start_removes_legacy_reply_keyboard_and_shows_inline_menu(self):
        update = SimpleNamespace(effective_message=SimpleNamespace(text="/start"),
                                 effective_chat=SimpleNamespace(id=1, type="private"),
                                 effective_user=SimpleNamespace(id=1))
        await self.ui.handle(update, SimpleNamespace(bot=self.bot))
        calls = self.bot.send_message.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].kwargs["reply_markup"].remove_keyboard)
        self.assertIsNotNone(calls[1].kwargs["reply_markup"].inline_keyboard)

    async def test_foreign_group_callbacks_do_not_mutate(self):
        row = self.store.add(1, 1, "Личная")
        screen = self.ui.card(row["id"], 1, 1)
        data = token(screen, "Выполнить")
        for update in (self.update(data, user=2), self.update(data, private=False)):
            await self.ui.handle_callback(update, SimpleNamespace(bot=self.bot))
            update.callback_query.answer.assert_awaited_once()
        self.assertEqual(self.store.get(row["id"], 1)["done"], 0)

    async def test_network_failure_after_save_does_not_duplicate(self):
        self.ui.begin(1, 1, Draft("add", "review", title="Один раз"))
        screen = self.ui.form(1, 1)
        key = token(screen, "Сохранить")
        update = self.update(key)
        update.callback_query.message.edit_text.side_effect = NetworkError("offline")
        self.bot.send_message.side_effect = NetworkError("offline")
        with self.assertLogs("buttons", level="ERROR"):
            await self.ui.handle_callback(update, SimpleNamespace(bot=self.bot))
        self.assertEqual(len(self.store.list_tasks(1)), 1)
        with self.assertRaises(InputError):
            self.ui.press(key, 1, 1)

    async def test_concurrent_clicks_only_save_once(self):
        screen = self.ui.begin(1, 1, Draft("add", "review", title="Один раз"))
        key = token(screen, "Сохранить")
        await asyncio.gather(*(self.ui.handle_callback(self.update(key), SimpleNamespace(bot=self.bot)) for _ in range(2)))
        self.assertEqual(len(self.store.list_tasks(1)), 1)

    async def test_due_reminder_contains_current_card_and_finish_buttons(self):
        row = self.store.add(1, 1, "Напоминание", description="Описание", remind_at=datetime.now(UTC).timestamp() - 60)
        await self.ui.send_due(self.bot)
        sent = self.bot.send_message.call_args.kwargs
        self.assertIn("Описание", sent["text"])
        markup = sent["reply_markup"]
        key = next(button.callback_data for line in markup.inline_keyboard for button in line if "Выполнить" in button.text)
        self.ui.press(key, 1, 1)
        self.assertEqual(self.store.get(row["id"], 1)["done"], 1)

    async def test_polling_requests_callback_updates(self):
        fake_app = SimpleNamespace(run_polling=lambda **kwargs: captured.update(kwargs))
        captured = {}
        with patch("bot.configure_logging"), patch("bot.Settings.load", return_value=Settings("123:fake", MOSCOW, Path("unused"))), \
                patch("bot.TaskStore", return_value=self.store), patch("bot.build_application", return_value=fake_app):
            self.assertEqual(main(), 0)
        self.assertEqual(captured["allowed_updates"], ["message", "callback_query"])
        # main закрыл соединение; новое временное соединение нужно только для tearDown.
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")


if __name__ == "__main__":
    unittest.main()
