"""Кнопочный интерфейс. Токены кнопок не содержат пользовательский текст или SQL.

Хранилище и доставка принадлежат сервису, здесь находятся диалоги и экраны.
Все входы из Telegram сериализуются той же блокировкой, что и доставка напоминаний.
"""

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import logging
import secrets
import sqlite3
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest, TelegramError

from errors import InputError
from task_dates import parse_deadline, quick_reminder

LOGGER = logging.getLogger(__name__)
UTC = timezone.utc
PAGE_SIZE = 5
FILTERS = {
    "active": "Активные", "done": "Выполненные", "all": "Все",
    "high": "Высокий приоритет", "reminders": "С напоминаниями",
    "today": "На сегодня", "overdue": "Просроченные",
}


@dataclass(frozen=True)
class View:
    filter: str = "active"
    page: int = 0
    group_id: int | None = None
    query: str | None = None


@dataclass
class Draft:
    kind: str
    step: str
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(8))
    target_id: int | None = None
    revision: int | None = None
    title: str = ""
    description: str = ""
    priority: int = 1
    done: bool = False
    group_id: int | None = None
    deadline_at: float | None = None
    deadline_has_time: bool = True
    remind_at: float | None = None
    history: list[str] = field(default_factory=list)
    page: int = 0
    generation: int = 0
    expires: float = field(default_factory=lambda: time.monotonic() + 1800)


@dataclass
class Session:
    view: View = field(default_factory=View)
    groups_page: int = 0
    draft: Draft | None = None
    epoch: int = 0


@dataclass(frozen=True)
class Action:
    owner: tuple[int, int]
    name: str
    args: tuple
    expires: float
    mutation: bool
    epoch: int
    draft: tuple[str, str, int] | None
    task: tuple[int, int] | None
    group: tuple[int, int] | None
    view: View


@dataclass(frozen=True)
class Screen:
    text: str
    markup: InlineKeyboardMarkup


class ButtonAssistant:
    def __init__(self, engine):
        self.engine = engine
        self.store, self.tz, self.lock = engine.store, engine.tz, engine.lock
        self.sessions: dict[tuple[int, int], Session] = {}
        # Ограниченный кэш. После перезапуска неизвестные кнопки безопасно отклоняются.
        self.actions: OrderedDict[str, Action] = OrderedDict()
        self.engine.reminder_markup = self.reminder_markup

    def session(self, user: int, chat: int) -> Session:
        return self.sessions.setdefault((user, chat), Session())

    def button(self, label, name, user, chat, *args, mutation=False, draft=False,
               task=None, group=None, view=None):
        session = self.session(user, chat)
        form = session.draft if draft else None
        now = time.monotonic()
        while self.actions and next(iter(self.actions.values())).expires <= now:
            self.actions.popitem(last=False)
        token = "b2:" + secrets.token_urlsafe(12)
        self.actions[token] = Action(
            (user, chat), name, args, now + (300 if mutation else 1800), mutation,
            session.epoch, (form.nonce, form.step, form.generation) if form else None,
            (task["id"], task["revision"]) if task is not None else None,
            (group["id"], group["revision"]) if group is not None else None,
            view or session.view,
        )
        while len(self.actions) > 5000:
            self.actions.popitem(last=False)
        return InlineKeyboardButton(label, callback_data=token)

    def navigation(self, user, chat):
        return [self.button("🏠 Меню", "home", user, chat)]

    def screen(self, text, rows, user, chat, *, menu=True):
        if menu:
            rows = rows + [self.navigation(user, chat)]
        return Screen(text, InlineKeyboardMarkup(rows))

    def home(self, user, chat, prefix=""):
        return self.screen(prefix + "Выберите действие. Названия и описания вводятся текстом.", [
            [self.button("➕ Добавить задачу", "add", user, chat, mutation=True)],
            [self.button("📋 Мои задачи", "list", user, chat, View())],
            [self.button("🔎 Поиск", "search", user, chat, mutation=True),
             self.button("📁 Группы", "groups", user, chat, 0)],
            [self.button("❓ Помощь", "help", user, chat)],
        ], user, chat, menu=False)

    def help(self, user, chat):
        text = self.engine.respond("/help", user, chat)
        text = text.replace("Отправьте текст, чтобы добавить задачу, или используйте команды:",
                            "Основные действия доступны кнопками. Команды остаются альтернативой:")
        text += ("\nОбычный текст вне формы ничего не создаёт. /delete и /group_delete требуют подтверждения."
                 "\nСрок — дата с необязательным временем; он независим от напоминания."
                 "\n/start или /menu — главное меню и отмена незавершённой формы.")
        return self.screen(text, [], user, chat)

    def cancel(self, user, chat):
        session = self.session(user, chat)
        session.draft = None
        session.epoch += 1
        return self.home(user, chat, "Диалог отменён. Незавершённые изменения не сохранены.\n\n")

    def begin(self, user, chat, draft):
        session = self.session(user, chat)
        replaced = session.draft is not None
        session.epoch += 1
        session.draft = draft
        screen = self.form(user, chat)
        return replace(screen, text=("Предыдущий незавершённый диалог заменён.\n\n" if replaced else "") + screen.text)

    def task_draft(self, kind, task_id, user):
        row = self.store.get(task_id, user)
        steps = {"edit": "title", "priority": "priority", "group": "group",
                 "deadline": "deadline", "reminder": "reminder", "delete": "review"}
        if kind == "reminder" and row["done"]:
            raise InputError("Задача выполнена. Новое напоминание для неё недоступно.")
        return Draft(kind, steps[kind], target_id=task_id, revision=row["revision"],
                     title=row["text"], description=row["description"], priority=row["priority"],
                     done=bool(row["done"]),
                     group_id=row["group_id"], deadline_at=row["deadline_at"],
                     deadline_has_time=bool(row["deadline_has_time"]), remind_at=row["remind_at"],
                     expires=time.monotonic() + (300 if kind == "delete" else 1800))

    def rows(self, user, view):
        return self.store.filtered_tasks(user, view.filter, self.tz, view.group_id, view.query)

    @staticmethod
    def page(items, index):
        total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
        index = min(max(0, index), total - 1)
        return items[index * PAGE_SIZE:(index + 1) * PAGE_SIZE], index, total

    @staticmethod
    def short(text, limit=55):
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def listing(self, user, chat, view=None):
        session = self.session(user, chat)
        view = view or session.view
        rows = self.rows(user, view)
        selected, index, pages = self.page(rows, view.page)
        view = replace(view, page=index)
        session.view = view
        title = FILTERS[view.filter]
        if view.group_id is not None:
            title += " · " + self.store.get_group(view.group_id, user)["name"]
        if view.query is not None:
            title += " · Поиск: " + self.short(view.query)
        text = f"{title}\nСтраница {index + 1}/{pages} · задач: {len(rows)}"
        keys = []
        for row in selected:
            caption = f"#{row['id']} · {self.short(row['text'])}"
            text += "\n" + caption
            keys.append([self.button(caption, "task", user, chat, row["id"], view=view)])
        if not selected:
            text += "\nСписок пуст. Выберите другой фильтр или добавьте задачу."
        navigation = []
        if index:
            navigation.append(self.button("← Предыдущая", "list", user, chat, replace(view, page=index - 1)))
        if index + 1 < pages:
            navigation.append(self.button("Следующая →", "list", user, chat, replace(view, page=index + 1)))
        if navigation:
            keys.append(navigation)
        filters = [self.button(label, "list", user, chat, replace(view, filter=name, page=0))
                   for name, label in FILTERS.items()]
        keys.extend([filters[i:i + 2] for i in range(0, len(filters), 2)])
        keys.append([self.button("➕ Добавить задачу", "add", user, chat, mutation=True)])
        if view.group_id is not None:
            keys.append([self.button("← К группе", "group_card", user, chat, view.group_id)])
        return self.screen(text, keys, user, chat)

    def card(self, task_id, user, chat, prefix=""):
        row = self.store.get(task_id, user)
        keys = []
        if not row["done"]:
            keys.append([self.button("✅ Выполнить", "done", user, chat, task_id, mutation=True, task=row)])
        for pair in (("✏️ Изменить", "edit", "🚦 Приоритет", "priority"),
                     ("📁 Группа", "group", "📅 Срок", "deadline")):
            keys.append([self.button(pair[0], "task_form", user, chat, pair[1], task_id, mutation=True, task=row),
                         self.button(pair[2], "task_form", user, chat, pair[3], task_id, mutation=True, task=row)])
        if not row["done"]:
            keys.append([self.button("⏰ Напоминание", "task_form", user, chat, "reminder", task_id,
                                     mutation=True, task=row)])
        keys.append([self.button("🗑 Удалить", "task_form", user, chat, "delete", task_id,
                                 mutation=True, task=row)])
        keys.append([self.button("← К списку", "list", user, chat, self.session(user, chat).view)])
        return self.screen(prefix + self.engine.format_task(row, show_created=True), keys, user, chat)

    def groups(self, user, chat, index=0):
        rows = self.store.list_groups(user)
        selected, index, pages = self.page(rows, index)
        self.session(user, chat).groups_page = index
        text = f"Мои группы · страница {index + 1}/{pages}"
        keys = []
        for row in selected:
            label = f"{self.short(row['name'])} · активных: {row['active_count']}"
            text += "\n" + label
            keys.append([self.button(label, "group_card", user, chat, row["id"])])
        if not rows:
            text += "\nГрупп пока нет."
        nav = []
        if index:
            nav.append(self.button("← Предыдущая", "groups", user, chat, index - 1))
        if index + 1 < pages:
            nav.append(self.button("Следующая →", "groups", user, chat, index + 1))
        if nav:
            keys.append(nav)
        keys.append([self.button("➕ Создать группу", "group_create", user, chat, mutation=True)])
        return self.screen(text, keys, user, chat)

    def group_card(self, group_id, user, chat, prefix=""):
        row = self.store.get_group(group_id, user)
        return self.screen(prefix + f"Группа «{row['name']}»", [
            [self.button("📋 Задачи группы", "list", user, chat, View("all", group_id=group_id))],
            [self.button("➕ Добавить новую задачу", "add_group", user, chat, group_id, mutation=True, group=row)],
            [self.button("Добавить существующую", "group_pick_task", user, chat, group_id, 0)],
            [self.button("✏️ Переименовать", "group_rename", user, chat, group_id, mutation=True, group=row),
             self.button("🗑 Удалить группу", "group_delete", user, chat, group_id, mutation=True, group=row)],
            [self.button("← К группам", "groups", user, chat, self.session(user, chat).groups_page)],
        ], user, chat)

    def group_pick_task(self, group_id, user, chat, index):
        group = self.store.get_group(group_id, user)
        selected, index, pages = self.page(self.store.list_tasks(user, True), index)
        keys = [[self.button(self.short(row["text"]), "assign_to_group", user, chat,
                             row["id"], group_id, mutation=True, task=row, group=group)] for row in selected]
        nav = []
        if index:
            nav.append(self.button("← Предыдущая", "group_pick_task", user, chat, group_id, index - 1))
        if index + 1 < pages:
            nav.append(self.button("Следующая →", "group_pick_task", user, chat, group_id, index + 1))
        if nav:
            keys.append(nav)
        keys.append([self.button("← К группе", "group_card", user, chat, group_id)])
        return self.screen(f"Выберите задачу для группы «{group['name']}» · {index + 1}/{pages}\n"
                           "Задача будет перенесена из прежней группы, если она была назначена.", keys, user, chat)

    def form(self, user, chat):
        draft = self.session(user, chat).draft
        if draft is None or draft.expires <= time.monotonic():
            raise InputError("Диалог завершён или устарел. Откройте новый через меню.")
        # Даже возврат к тому же шагу не оживляет кнопки старого подтверждения.
        draft.generation += 1
        def choice(label, value):
            return self.button(label, "choose", user, chat, value, mutation=True, draft=True)
        keys = []
        step = draft.step
        if step == "title":
            text = "Введите название задачи"
            if draft.kind == "edit":
                text += "\nТекущее: " + draft.title
                keys.append([choice("Оставить", "keep")])
        elif step == "description":
            text = "Введите описание"
            if draft.kind == "edit":
                text += "\nТекущее: " + (draft.description or "(без описания)")
                keys.append([choice("Оставить", "keep"), choice("Очистить", "none")])
            else:
                keys.append([choice("Пропустить", "none")])
        elif step == "priority":
            text = "Укажите приоритет (высокий, средний, низкий)"
            keys.append([choice(label, str(index)) for index, label in enumerate(self.engine.priority_labels)])
        elif step == "group":
            text = "Выберите группу для задачи"
            if draft.group_id is not None:
                try:
                    group = self.store.get_group(draft.group_id, user)
                except InputError:
                    text += "\nРанее выбранная группа удалена. Выберите другую или оставьте без группы."
                else:
                    text += "\nВыбрана: " + group["name"]
                    keys.append([choice("Оставить выбранную группу", f"g{group['id']}")])
            selected, index, pages = self.page(self.store.list_groups(user), draft.page)
            draft.page = index
            for group in selected:
                keys.append([choice(self.short(group["name"]), f"g{group['id']}")])
            if pages > 1:
                nav = []
                if index:
                    nav.append(choice("← Предыдущая", "page-"))
                if index + 1 < pages:
                    nav.append(choice("Следующая →", "page+"))
                keys.append(nav)
            keys.append([choice("Пропустить" if draft.kind == "add" else "Без группы", "none")])
        elif step == "deadline":
            text = f"Укажите срок выполнения ({self.tz.key})\nБез времени — до конца выбранного дня."
            keys.extend([[choice("Сегодня", "today"), choice("Завтра", "tomorrow")],
                         [choice("Выбрать дату", "date"), choice("Дата и время", "datetime")],
                         [choice("Пропустить" if draft.kind == "add" else "Убрать срок", "none")]])
        elif step in ("deadline_date", "deadline_time"):
            text = "Введите ДД.ММ или ДД.ММ.ГГГГ"
            if step == "deadline_time":
                text += " ЧЧ:ММ"
            text += f"\nКороткая дата — текущий год; часовой пояс: {self.tz.key}."
        elif step == "reminder":
            text = f"Установить напоминание ({self.tz.key})\nВыберите время или введите ДД.ММ ЧЧ:ММ."
            keys.extend([[choice("Через 15 минут", "15"), choice("Через час", "60")],
                         [choice("Завтра в 09:00", "tomorrow"), choice("Ввести дату", "date")]])
            if draft.deadline_at is not None and draft.deadline_at - 3600 > datetime.now(UTC).timestamp():
                when = self.engine.format_time(draft.deadline_at - 3600)
                text += f"\nЗа час до срока: {when}. Без нажатия напоминание не включится."
                keys.append([choice("За час до срока", "before")])
            keys.append([choice("Пропустить" if draft.kind == "add" else "Отключить", "none")])
        elif step == "reminder_date":
            text = f"Введите будущую дату: ДД.ММ ЧЧ:ММ или ДД.ММ.ГГГГ ЧЧ:ММ ({self.tz.key})."
        elif step == "group_name":
            text = "Введите название группы"
            if draft.kind == "group_rename":
                text += "\nТекущее: " + draft.title
        elif step == "search":
            text = "Введите часть названия или описания задачи"
        elif step == "review":
            if draft.kind == "delete":
                row = self.store.get(draft.target_id, user)
                text = "Удалить задачу?\n" + self.engine.format_task(row)
                text += "\nЗадача и её напоминание будут удалены."
                keys.append([choice("Да, удалить", "save")])
            elif draft.kind == "group_delete":
                row = self.store.get_group(draft.target_id, user)
                text = f"Удалить группу «{row['name']}»?\nЕё задачи останутся без группы. Напоминания сохранятся."
                keys.append([choice("Да, удалить", "save")])
            else:
                text = self.preview(draft, user)
                keys.append([choice("💾 Сохранить", "save")])
        else:
            raise InputError("Неизвестный шаг диалога.")
        if draft.history:
            keys.append([self.button("← Назад", "back", user, chat, mutation=True, draft=True)])
        keys.append([self.button("Отмена", "cancel", user, chat, mutation=True, draft=True)])
        return self.screen(text, keys, user, chat)

    def preview(self, draft, user):
        if draft.kind.startswith("group_"):
            return f"Проверьте название группы: {draft.title}"
        if draft.kind == "search":
            return draft.title
        text = f"Проверьте данные:\n{draft.title}"
        if draft.description:
            text += "\nОписание: " + draft.description
        text += "\n" + self.engine.priority_labels[draft.priority]
        group_name = "Без группы"
        if draft.group_id is not None:
            try:
                group_name = self.store.get_group(draft.group_id, user)["name"]
            except InputError:
                # Черновик не изменяется молча: сохранение вернёт на выбор группы.
                group_name = "выбранная группа удалена; выберите другую перед сохранением"
        text += "\nГруппа: " + group_name
        if draft.deadline_at is not None:
            from task_dates import deadline_day
            label = self.engine.format_time(draft.deadline_at) if draft.deadline_has_time else (
                deadline_day(draft.deadline_at, False, self.tz).strftime("%d.%m.%Y") + " — до конца дня"
            )
            text += "\nСрок: " + label
            if not draft.done and draft.deadline_at <= datetime.now(UTC).timestamp():
                text += " · ПРОСРОЧЕНА"
        else:
            text += "\nСрок: не задан"
        text += "\nНапоминание: " + (self.engine.format_time(draft.remind_at) if draft.remind_at is not None else "не задано")
        if draft.kind == "deadline" and draft.remind_at is not None:
            text += "\nСуществующее напоминание не переносится. Его можно изменить в карточке."
        return text

    def advance(self, draft, step):
        draft.history.append(draft.step)
        draft.step = step

    def choose(self, value, user, chat):
        draft = self.session(user, chat).draft
        if value == "save" and draft.step == "review":
            return self.save(user, chat)
        step = draft.step
        if step == "title" and draft.kind == "edit" and value == "keep":
            self.advance(draft, "description")
        elif step == "description" and value in ("none", "keep"):
            if value == "none":
                draft.description = ""
            self.advance(draft, "priority" if draft.kind == "add" else "review")
        elif step == "priority" and value in ("0", "1", "2"):
            draft.priority = int(value)
            self.advance(draft, "group" if draft.kind == "add" else "review")
        elif step == "group":
            if value in ("page-", "page+"):
                draft.page += -1 if value == "page-" else 1
                return self.form(user, chat)
            if value == "none":
                draft.group_id = None
            elif value.startswith("g"):
                group = self.store.get_group(self.engine.parse_id(value[1:]), user)
                draft.group_id = group["id"]
            else:
                raise InputError("Выберите группу кнопкой.")
            self.advance(draft, "deadline" if draft.kind == "add" else "review")
        elif step == "deadline":
            if value in ("date", "datetime"):
                self.advance(draft, "deadline_date" if value == "date" else "deadline_time")
            else:
                if value == "none":
                    draft.deadline_at, draft.deadline_has_time = None, True
                elif value in ("today", "tomorrow"):
                    day = datetime.now(self.tz).date() + timedelta(days=int(value == "tomorrow"))
                    draft.deadline_at, draft.deadline_has_time = parse_deadline(day.strftime("%d.%m.%Y"), self.tz)
                else:
                    raise InputError("Выберите срок кнопкой.")
                self.advance(draft, "reminder" if draft.kind == "add" else "review")
        elif step == "reminder":
            if value == "date":
                self.advance(draft, "reminder_date")
            else:
                draft.remind_at = None if value == "none" else quick_reminder(value, self.tz, draft.deadline_at)
                self.advance(draft, "review")
        else:
            raise InputError("Эта кнопка не относится к текущему шагу.")
        return self.form(user, chat)

    def text_input(self, text, user, chat):
        session = self.session(user, chat)
        draft = session.draft
        if draft is None:
            return self.home(user, chat, "Для создания задачи нажмите «Добавить задачу» или отправьте /add.\n\n")
        if draft.expires <= time.monotonic():
            session.draft = None
            session.epoch += 1
            return self.home(user, chat, "Незавершённый диалог устарел. Начните заново.\n\n")
        text = text.strip()
        step = draft.step
        if step == "title":
            self.engine.validate_text(text)
            draft.title = text
            self.advance(draft, "description")
        elif step == "description":
            self.engine.validate_text(draft.title, text)
            draft.description = text
            self.advance(draft, "priority" if draft.kind == "add" else "review")
        elif step in ("deadline_date", "deadline_time"):
            when, has_time = parse_deadline(text, self.tz)
            if (step == "deadline_time") != has_time:
                raise InputError("Для этого шага " + ("добавьте время ЧЧ:ММ." if step == "deadline_time" else "введите дату без времени."))
            draft.deadline_at, draft.deadline_has_time = when, has_time
            self.advance(draft, "reminder" if draft.kind == "add" else "review")
        elif step in ("reminder", "reminder_date"):
            draft.remind_at = self.engine.parse_reminder(text, self.tz)
            self.advance(draft, "review")
        elif step == "group_name":
            draft.title = self.engine.validate_group_name(text)
            self.advance(draft, "review")
        elif step == "search":
            if not text or len(text.encode("utf-16-le")) // 2 > 3000:
                raise InputError("Введите часть названия или описания, до 3000 символов.")
            # Запрос чтения проверяется до завершения диалога; при ошибке ввод можно повторить.
            self.store.search(user, text)
            view = View("all", query=text)
            session.draft = None
            session.epoch += 1
            return self.listing(user, chat, view)
        else:
            raise InputError("На этом шаге выберите действие кнопкой; текст ничего не изменил.")
        return self.form(user, chat)

    def check_revision(self, kind, target, revision, user):
        row = self.store.get_group(target, user) if kind.startswith("group_") else self.store.get(target, user)
        if row["revision"] != revision:
            raise InputError("Данные изменились. Откройте актуальную карточку и повторите действие.")
        return row

    def save(self, user, chat):
        session = self.session(user, chat)
        draft = session.draft
        if draft.kind != "add" and not draft.kind.startswith("group_"):
            self.check_revision(draft.kind, draft.target_id, draft.revision, user)
        elif draft.kind in ("group_rename", "group_delete"):
            self.check_revision(draft.kind, draft.target_id, draft.revision, user)
        if draft.kind in ("add", "reminder") and draft.remind_at is not None:
            if draft.remind_at <= datetime.now(UTC).timestamp():
                # Возвращаем на выбор времени: пользователь может исправить только это поле.
                self.advance(draft, "reminder")
                raise InputError("Время напоминания уже прошло. Выберите новое время или пропустите.")
        if draft.kind == "add" and draft.group_id is not None:
            try:
                self.store.get_group(draft.group_id, user)
            except InputError as exc:
                draft.group_id = None
                self.advance(draft, "group")
                raise InputError("Выбранная группа удалена. Выберите другую группу или пропустите.") from exc
        target = draft.target_id
        kind = draft.kind
        if kind == "add":
            row = self.store.add(user, chat, draft.title, draft.description, draft.priority,
                                 draft.remind_at, draft.group_id, draft.deadline_at, draft.deadline_has_time)
            target = row["id"]
        elif kind == "edit":
            self.store.update_text(target, user, draft.title, draft.description)
        elif kind == "priority":
            self.store.set_priority(target, user, draft.priority)
        elif kind == "group":
            self.store.assign_group(target, user, draft.group_id)
        elif kind == "deadline":
            self.store.set_deadline(target, user, draft.deadline_at, draft.deadline_has_time)
        elif kind == "reminder":
            self.store.set_reminder(target, user, draft.remind_at)
        elif kind == "delete":
            self.store.delete(target, user)
        elif kind == "group_create":
            target = self.store.create_group(user, draft.title)["id"]
        elif kind == "group_rename":
            self.store.rename_group(target, user, draft.title)
        elif kind == "group_delete":
            self.store.delete_group(target, user)
        else:
            raise InputError("Неизвестная операция.")
        # Фиксируем завершение ДО отправки экрана: сетевой сбой не повторяет запись.
        session.draft = None
        session.epoch += 1
        if kind == "group_delete":
            if session.view.group_id == target:
                session.view = View()
            return self.groups(user, chat, session.groups_page)
        if kind.startswith("group_"):
            return self.group_card(target, user, chat, "Сохранено.\n")
        if kind == "delete":
            screen = self.listing(user, chat)
            return replace(screen, text="Задача удалена.\n" + screen.text)
        prefix = "Задача добавлена!\n" if kind == "add" else "Изменения сохранены.\n"
        if kind == "deadline" and draft.remind_at is not None:
            prefix += "Существующее напоминание не перенесено. Изменить его можно кнопкой «Напоминание».\n"
        return self.card(target, user, chat, prefix)

    def respond(self, text, user, chat):
        if text is None:
            return self.home(user, chat, "Пожалуйста, пришлите текст.\n\n")
        if not text.strip().startswith("/"):
            return self.text_input(text, user, chat)
        parts = text.strip().split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        body = parts[1].strip() if len(parts) > 1 else ""
        args = body.split()
        session = self.session(user, chat)
        if command in ("/start", "/menu"):
            session.draft = None
            session.epoch += 1
            return self.home(user, chat, "Привет! Я помогу вести список дел.\n\n")
        if command == "/help":
            return self.help(user, chat)
        if command == "/cancel":
            if args:
                raise InputError("Отправьте /cancel без параметров.")
            return self.cancel(user, chat)
        if command == "/add":
            if not body:
                return self.begin(user, chat, Draft("add", "title"))
            row = self.store.add(user, chat, body)
            session.draft = None
            session.epoch += 1
            return self.card(row["id"], user, chat, "Задача добавлена!\n")
        if command in ("/list", "/list_all"):
            if args:
                raise InputError("Отправьте /list или /list_all без параметров.")
            return self.listing(user, chat, View("all" if command == "/list_all" else "active"))
        if command == "/search":
            if not body:
                return self.begin(user, chat, Draft("search", "search"))
            if len(body.encode("utf-16-le")) // 2 > 3000:
                raise InputError("Поисковый запрос слишком длинный.")
            return self.listing(user, chat, View("all", query=body))
        if command == "/groups":
            if args:
                raise InputError("Отправьте /groups без параметров.")
            return self.groups(user, chat)
        if command == "/group_create":
            if args:
                raise InputError("Отправьте /group_create без параметров.")
            return self.begin(user, chat, Draft("group_create", "group_name"))
        if command in ("/edit", "/delete", "/task_group", "/group_tasks", "/group_rename", "/group_delete"):
            if len(args) != 1:
                raise InputError(f"Формат: {command} <id>.")
            target = self.engine.parse_id(args[0])
            if command == "/group_tasks":
                return self.listing(user, chat, View("all", group_id=target))
            if command in ("/group_rename", "/group_delete"):
                row = self.store.get_group(target, user)
                return self.begin(user, chat, Draft(command[1:], "group_name" if command == "/group_rename" else "review",
                                                   target_id=target, revision=row["revision"], title=row["name"],
                                                   expires=time.monotonic() + (300 if command == "/group_delete" else 1800)))
            kind = {"/edit": "edit", "/delete": "delete", "/task_group": "group"}[command]
            return self.begin(user, chat, self.task_draft(kind, target, user))
        if command in ("/done", "/priority", "/remind", "/remind_off", "/task_ungroup"):
            # Одна и та же бизнес-логика/валидация у команд и кнопок.
            response = self.engine.respond(text, user, chat)
            session.draft = None
            session.epoch += 1
            return self.card(self.engine.parse_id(args[0]), user, chat, response + "\n\n")
        return self.home(user, chat, "Неизвестная команда. Помощь доступна кнопкой или через /help.\n\n")

    def press(self, data, user, chat):
        action = self.actions.get(data) if isinstance(data, str) else None
        if action is None or action.owner != (user, chat) or action.expires <= time.monotonic():
            raise InputError("Кнопка устарела или недоступна. Откройте меню через /start.")
        session = self.session(user, chat)
        if action.mutation and action.epoch != session.epoch:
            raise InputError("Кнопка относится к прежнему диалогу. Откройте актуальный экран.")
        if action.draft is not None:
            draft = session.draft
            if draft is None or (draft.nonce, draft.step, draft.generation) != action.draft or draft.expires <= time.monotonic():
                raise InputError("Этот шаг диалога уже завершён. Откройте актуальный экран.")
        if action.task is not None:
            self.check_revision("task", *action.task, user)
        if action.group is not None:
            self.check_revision("group_action", *action.group, user)
        screen = self.dispatch(action, user, chat)
        if action.mutation:
            self.actions.pop(data, None)
        return screen

    def dispatch(self, action, user, chat):
        name, args = action.name, action.args
        session = self.session(user, chat)
        if name == "home":
            session.draft = None
            session.epoch += 1
            return self.home(user, chat)
        if name == "help":
            return self.help(user, chat)
        if name == "cancel":
            return self.cancel(user, chat)
        if name == "back":
            draft = session.draft
            if not draft.history:
                raise InputError("Предыдущего шага нет.")
            draft.step = draft.history.pop()
            return self.form(user, chat)
        if name == "add":
            return self.begin(user, chat, Draft("add", "title"))
        if name == "add_group":
            return self.begin(user, chat, Draft("add", "title", group_id=args[0]))
        if name == "search":
            return self.begin(user, chat, Draft("search", "search"))
        if name == "list":
            return self.listing(user, chat, args[0])
        if name == "task":
            session.view = action.view
            return self.card(args[0], user, chat)
        if name == "task_form":
            session.view = action.view
            return self.begin(user, chat, self.task_draft(args[0], args[1], user))
        if name == "choose":
            return self.choose(args[0], user, chat)
        if name == "groups":
            return self.groups(user, chat, args[0])
        if name == "group_card":
            return self.group_card(args[0], user, chat)
        if name == "group_pick_task":
            return self.group_pick_task(args[0], user, chat, args[1])
        if name == "group_create":
            return self.begin(user, chat, Draft("group_create", "group_name"))
        if name in ("group_rename", "group_delete"):
            row = self.store.get_group(args[0], user)
            return self.begin(user, chat, Draft(name, "group_name" if name == "group_rename" else "review",
                                               target_id=row["id"], revision=row["revision"], title=row["name"],
                                               expires=time.monotonic() + (300 if name == "group_delete" else 1800)))
        if name in ("done", "assign_to_group"):
            if name == "done":
                row = self.store.get(args[0], user)
                if row["done"]:
                    raise InputError("Задача уже выполнена.")
                self.store.finish(args[0], user)
            else:
                self.store.assign_group(args[0], user, args[1])
            session.draft = None
            session.epoch += 1
            session.view = action.view
            return self.card(args[0], user, chat, "Изменения сохранены.\n")
        raise InputError("Неизвестная кнопка.")

    def reminder_markup(self, row):
        user, chat = row["user_id"], row["chat_id"]
        return InlineKeyboardMarkup([
            [self.button("Открыть задачу", "task", user, chat, row["id"])],
            [self.button("✅ Выполнить", "done", user, chat, row["id"], mutation=True, task=row)],
            self.navigation(user, chat),
        ])

    async def send_due(self, telegram_bot):
        await self.engine.send_due(telegram_bot)

    def recovery(self, error, user, chat):
        try:
            screen = self.form(user, chat) if self.session(user, chat).draft else self.home(user, chat)
        except (InputError, sqlite3.Error):
            screen = self.home(user, chat)
        return replace(screen, text=str(error) + "\n\n" + screen.text)

    async def send_screen(self, screen, chat, bot, message=None, edit=False, clear_keyboard=False):
        # Только последнее сообщение получает кнопки: невозможно нажать одну запись дважды
        # из двух частей длинного ответа. Пользовательские строки отправляются без parse_mode.
        if clear_keyboard:
            # Удаляем reply-клавиатуру старой версии; основное меню теперь inline.
            await bot.send_message(chat_id=chat, text="Управление — кнопками под сообщениями.",
                                   reply_markup=ReplyKeyboardRemove())
        chunks = self.engine_split(screen.text)
        if edit and len(chunks) == 1 and message is not None:
            try:
                await message.edit_text(chunks[0], reply_markup=screen.markup)
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
            except (TelegramError, TypeError, AttributeError):
                pass
        for index, chunk in enumerate(chunks):
            await bot.send_message(chat_id=chat, text=chunk,
                                   reply_markup=screen.markup if index == len(chunks) - 1 else None)

    @staticmethod
    def engine_split(text):
        result, current, size = [], "", 0
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if size + width > 4000:
                result.append(current)
                current, size = "", 0
            current += char
            size += width
        if current:
            result.append(current)
        return result

    async def handle(self, update, context):
        message, chat, user = update.effective_message, update.effective_chat, update.effective_user
        if message is None or chat is None or user is None:
            return
        if chat.type != "private":
            await message.reply_text("Я веду личные списки задач. Напишите мне в личный чат.")
            return
        if message.text and message.text.startswith("/"):
            command = message.text.split(maxsplit=1)[0]
            if "@" in command and command.split("@", 1)[1].lower() != context.bot.username.lower():
                return
        try:
            async with self.lock:
                screen = self.respond(message.text, user.id, chat.id)
        except InputError as exc:
            screen = self.recovery(exc, user.id, chat.id)
        except sqlite3.Error:
            LOGGER.exception("Ошибка SQLite в кнопочном интерфейсе")
            screen = self.recovery("Не удалось сохранить или прочитать данные. Проверьте результат в списке задач.", user.id, chat.id)
        try:
            command = message.text.split(maxsplit=1)[0].split("@", 1)[0].lower() if message.text else ""
            await self.send_screen(screen, chat.id, context.bot, clear_keyboard=command in ("/start", "/menu"))
        except TelegramError:
            LOGGER.exception("Не удалось отправить экран; проверьте актуальные данные при следующем открытии меню")

    async def handle_callback(self, update, context):
        query, chat, user = update.callback_query, update.effective_chat, update.effective_user
        if query is None:
            return
        try:
            await query.answer()
        except TelegramError:
            LOGGER.warning("Не удалось подтвердить нажатие кнопки")
        if chat is None or user is None or chat.type != "private":
            return
        try:
            async with self.lock:
                screen = self.press(query.data, user.id, chat.id)
        except InputError as exc:
            screen = self.recovery(exc, user.id, chat.id)
        except sqlite3.Error:
            LOGGER.exception("Ошибка SQLite при нажатии кнопки")
            screen = self.recovery("Не удалось сохранить или прочитать данные. Проверьте результат в списке задач.", user.id, chat.id)
        try:
            await self.send_screen(screen, chat.id, context.bot, query.message, edit=True)
        except TelegramError:
            LOGGER.exception("Не удалось отправить экран после нажатия; данные проверяются при следующем открытии")
