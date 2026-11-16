"""
Telegram-бот «Список дел с дедлайнами».

Как пользоваться:
    • Отправьте боту текст дела, например:  Сдать реферат по ТГП
      → бот покажет календарь, нажмите на дату дедлайна
      → дело сохранится, и бот пришлёт обновлённый список
    • /list      — список дел, отсортированный по дате
    • /del 3     — удалить дело №3 (можно несколько: /del 2 5 7)
    • /help      — подсказка

Каждый день в 00:00 (МСК) бот присылает дела с дедлайном на сегодня
и просроченные дела. Через 3 дня после дедлайна дело удаляется само
(например, дедлайн 16.11 → удалится в 00:00 19.11).

Установка:  pip install aiogram tzdata
Запуск:     вставьте токен в TOKEN (или задайте переменную окружения BOT_TOKEN)
            python todo_bot.py
"""

import asyncio
import calendar
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ─── Настройки ────────────────────────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
TZ = ZoneInfo("Europe/Moscow")   # дайджест приходит в 00:00 по этому поясу
CHECK_EVERY_SEC = 20             # как часто проверять, не наступила ли полночь
AUTO_DELETE_AFTER_DAYS = 3       # через сколько дней после дедлайна дело удаляется
DB_PATH = "todo.db"

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# ─── База данных ──────────────────────────────────────────────────────────────
db = sqlite3.connect(DB_PATH)
db.execute(
    """CREATE TABLE IF NOT EXISTS tasks (
           id      INTEGER PRIMARY KEY AUTOINCREMENT,
           chat_id INTEGER NOT NULL,
           text    TEXT    NOT NULL,
           due     TEXT    NOT NULL            -- дата в формате ГГГГ-ММ-ДД
       )"""
)
db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
db.commit()


def today() -> date:
    return datetime.now(TZ).date()


def add_task(chat_id: int, text: str, due: date) -> None:
    db.execute(
        "INSERT INTO tasks (chat_id, text, due) VALUES (?, ?, ?)",
        (chat_id, text, due.isoformat()),
    )
    db.commit()


def get_tasks(chat_id: int) -> list[tuple[int, str, date]]:
    """Все дела пользователя, отсортированные по дате."""
    rows = db.execute(
        "SELECT id, text, due FROM tasks WHERE chat_id = ? ORDER BY due, id",
        (chat_id,),
    ).fetchall()
    return [(i, t, date.fromisoformat(d)) for i, t, d in rows]


def delete_tasks(ids: list[int]) -> None:
    db.executemany("DELETE FROM tasks WHERE id = ?", [(i,) for i in ids])
    db.commit()


def purge_old_tasks(day: date) -> dict[int, list[tuple[str, date]]]:
    """Удаляет дела, у которых дедлайн был AUTO_DELETE_AFTER_DAYS и более дней назад.
    Возвращает удалённое по чатам, чтобы сообщить пользователю."""
    border = (day - timedelta(days=AUTO_DELETE_AFTER_DAYS)).isoformat()
    rows = db.execute(
        "SELECT chat_id, text, due FROM tasks WHERE due <= ? ORDER BY due, id", (border,)
    ).fetchall()
    db.execute("DELETE FROM tasks WHERE due <= ?", (border,))
    db.commit()
    removed: dict[int, list[tuple[str, date]]] = {}
    for chat_id, text, due in rows:
        removed.setdefault(chat_id, []).append((text, date.fromisoformat(due)))
    return removed


def get_meta(key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    db.commit()


# ─── Оформление ───────────────────────────────────────────────────────────────
def escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_date(d: date) -> str:
    """Единый формат даты: 25.11.2026 (ср)."""
    return f"{d:%d.%m.%Y} ({WEEKDAYS[d.weekday()]})"


def render_list(chat_id: int) -> str:
    tasks = get_tasks(chat_id)
    if not tasks:
        return "📭 Список дел пуст."
    t = today()
    lines = ["📋 <b>Ваши дела:</b>"]
    for n, (_, text, due) in enumerate(tasks, 1):
        mark = "⚠️ " if due < t else "🔥 " if due == t else ""
        lines.append(f"{n}. {mark}<b>{fmt_date(due)}</b> — {escape(text)}")
    lines.append("\n<i>Удалить: /del номер</i>")
    return "\n".join(lines)


HELP = (
    "Я веду ваш список дел с дедлайнами 📋\n\n"
    "<b>Добавить дело</b> — просто напишите его текст, "
    "например <code>Сдать реферат</code>, и выберите дату в календаре.\n\n"
    "/list — список дел по дате\n"
    "/del 3 — удалить дело №3 (можно: /del 2 5 7)\n"
    "/help — эта подсказка\n\n"
    "🌙 Каждый день в 00:00 я присылаю дедлайны на сегодня и просроченные дела.\n"
    f"🧹 Через {AUTO_DELETE_AFTER_DAYS} дня после дедлайна дело удаляется само.\n"
    "🔥 — дедлайн сегодня, ⚠️ — просрочено."
)

# ─── Календарь ────────────────────────────────────────────────────────────────
def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    t = today()
    rows = [
        [_btn(f"{MONTHS[month - 1]} {year}", "cal:ignore")],
        [_btn(w, "cal:ignore") for w in WEEKDAYS],
    ]

    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(_btn(" ", "cal:ignore"))
                continue
            d = date(year, month, day)
            if d < t:                                   # прошедшие дни недоступны
                row.append(_btn("·", "cal:ignore"))
            else:
                label = f"[{day}]" if d == t else str(day)
                row.append(_btn(label, f"cal:day:{d.isoformat()}"))
        rows.append(row)

    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    can_go_back = (year, month) > (t.year, t.month)
    rows.append([
        _btn("◀️", f"cal:nav:{prev_y}-{prev_m}") if can_go_back else _btn(" ", "cal:ignore"),
        _btn("▶️", f"cal:nav:{next_y}-{next_m}"),
    ])
    rows.append([
        _btn("Сегодня", f"cal:day:{t.isoformat()}"),
        _btn("Завтра", f"cal:day:{(t + timedelta(days=1)).isoformat()}"),
        _btn("✖️ Отмена", "cal:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Текст дела, ожидающего выбора даты: (chat_id, id сообщения с календарём) -> текст
pending: dict[tuple[int, int], str] = {}

# ─── Обработчики ──────────────────────────────────────────────────────────────
dp = Dispatcher()


@dp.message(CommandStart())
@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP)


@dp.message(Command("list"))
async def cmd_list(message: Message):
    await message.answer(render_list(message.chat.id))


@dp.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    tasks = get_tasks(message.chat.id)
    if not command.args:
        await message.answer("Укажите номер: <code>/del 3</code>")
        return

    numbers = [p for p in command.args.replace(",", " ").split() if p.isdigit()]
    bad = [n for n in numbers if not 1 <= int(n) <= len(tasks)]
    if not numbers or bad:
        await message.answer(
            f"Нет дела с номером {', '.join(bad) or escape(command.args)}. "
            f"Сейчас в списке {len(tasks)} — посмотрите /list"
        )
        return

    ids = [tasks[int(n) - 1][0] for n in set(numbers)]
    delete_tasks(ids)
    await message.answer(f"🗑 Удалено: {len(ids)}\n\n" + render_list(message.chat.id))


async def ask_date(message: Message, text: str):
    t = today()
    sent = await message.answer(
        f"📅 Выберите дедлайн для:\n<b>{escape(text)}</b>",
        reply_markup=build_calendar(t.year, t.month),
    )
    pending[(sent.chat.id, sent.message_id)] = text


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Напишите текст дела, например: <code>Сдать реферат</code>")
        return
    await ask_date(message, command.args.strip())


@dp.message(F.text & ~F.text.startswith("/"))
async def plain_text(message: Message):
    await ask_date(message, message.text.strip())


@dp.callback_query(F.data.startswith("cal:"))
async def on_calendar(cb: CallbackQuery):
    _, action, *rest = cb.data.split(":")
    chat_id = cb.message.chat.id
    key = (chat_id, cb.message.message_id)

    if action == "nav":
        year, month = map(int, rest[0].split("-"))
        await cb.message.edit_reply_markup(reply_markup=build_calendar(year, month))

    elif action == "cancel":
        pending.pop(key, None)
        await cb.message.edit_text("✖️ Добавление отменено.")

    elif action == "day":
        text = pending.pop(key, None)
        if text is None:
            await cb.answer("Это окно устарело — отправьте дело ещё раз.", show_alert=True)
            return
        due = date.fromisoformat(rest[0])
        add_task(chat_id, text, due)
        await cb.message.edit_text(f"✅ Добавлено: <b>{fmt_date(due)}</b> — {escape(text)}")
        await cb.message.answer(render_list(chat_id))      # список заново после добавления

    await cb.answer()


# ─── Ежедневный дайджест в 00:00 ──────────────────────────────────────────────
async def send_digests(bot: Bot, day: date):
    removed = purge_old_tasks(day)          # сначала убираем старые просроченные дела

    chats = {c for (c,) in db.execute(
        "SELECT DISTINCT chat_id FROM tasks WHERE due <= ?", (day.isoformat(),)
    ).fetchall()} | set(removed)

    for chat_id in chats:
        tasks = get_tasks(chat_id)
        due_today = [t for _, t, d in tasks if d == day]
        overdue = [(t, d) for _, t, d in tasks if d < day]

        lines = []
        if removed.get(chat_id):
            lines.append(f"🧹 <b>Удалено автоматически</b> (прошло {AUTO_DELETE_AFTER_DAYS} дня после дедлайна):")
            lines += [f"• {d:%d.%m.%Y} — {escape(t)}" for t, d in removed[chat_id]]
            lines.append("")
        if due_today:
            lines.append(f"🌙 <b>Дедлайны на сегодня, {fmt_date(day)}:</b>")
            lines += [f"🔥 {escape(t)}" for t in due_today]
        if overdue:
            if lines and lines[-1]:
                lines.append("")
            lines.append("⚠️ <b>Просрочено:</b>")
            lines += [f"• {d:%d.%m.%Y} — {escape(t)}" for t, d in overdue]
        while lines and not lines[-1]:
            lines.pop()
        lines.append("\nВесь список: /list")

        try:
            await bot.send_message(chat_id, "\n".join(lines))
        except Exception:
            logging.exception("Не удалось отправить дайджест в чат %s", chat_id)


async def daily_digest_loop(bot: Bot):
    # При самом первом запуске считаем, что сегодняшний дайджест уже был,
    # чтобы первый пришёл в ближайшую полночь, а не сразу.
    if get_meta("last_digest") is None:
        set_meta("last_digest", today().isoformat())

    while True:
        t = today()
        if t.isoformat() > get_meta("last_digest"):    # наступили новые сутки
            await send_digests(bot, t)
            set_meta("last_digest", t.isoformat())
        await asyncio.sleep(CHECK_EVERY_SEC)


# ─── Запуск ───────────────────────────────────────────────────────────────────
async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    asyncio.create_task(daily_digest_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
