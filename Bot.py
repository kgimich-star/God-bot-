import asyncio
import logging
import os
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ForceReply,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing!")

if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID must be a numeric Telegram ID!")

ADMIN_ID = int(ADMIN_ID_RAW)

DB_FILE = "god_poll.db"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================================================
# BOT
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =========================================================
# DATABASE
# =========================================================

def db():
    return sqlite3.connect(DB_FILE)


def init_db():

    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS names (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(chat_id, user_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            drop_active INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS polls (
            poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            question TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS poll_options (
            poll_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            votes INTEGER DEFAULT 0,
            PRIMARY KEY(poll_id, option_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS poll_votes (
            poll_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            PRIMARY KEY(poll_id, user_id)
        )
    """)

    conn.commit()
    conn.close()


# =========================================================
# NAME FUNCTIONS
# =========================================================

def add_or_update_name(chat_id, user_id, name):

    conn = db()

    row = conn.execute(
        """
        SELECT id FROM names
        WHERE chat_id=? AND user_id=?
        """,
        (chat_id, user_id)
    ).fetchone()

    if row:

        conn.execute(
            """
            UPDATE names
            SET name=?
            WHERE chat_id=? AND user_id=?
            """,
            (name, chat_id, user_id)
        )

        result = "updated"

    else:

        conn.execute(
            """
            INSERT INTO names(chat_id,user_id,name)
            VALUES(?,?,?)
            """,
            (chat_id, user_id, name)
        )

        result = "added"

    conn.commit()
    conn.close()

    return result


def get_names(chat_id):

    conn = db()

    rows = conn.execute(
        """
        SELECT user_id, name
        FROM names
        WHERE chat_id=?
        ORDER BY id ASC
        """,
        (chat_id,)
    ).fetchall()

    conn.close()

    return rows


def clear_names(chat_id):

    conn = db()

    conn.execute(
        "DELETE FROM names WHERE chat_id=?",
        (chat_id,)
    )

    conn.commit()
    conn.close()


# =========================================================
# DROP STATUS
# =========================================================

def set_drop(chat_id, active):

    conn = db()

    conn.execute(
        """
        INSERT INTO settings(chat_id,drop_active)
        VALUES(?,?)
        ON CONFLICT(chat_id)
        DO UPDATE SET drop_active=excluded.drop_active
        """,
        (chat_id, int(active))
    )

    conn.commit()
    conn.close()


def drop_active(chat_id):

    conn = db()

    row = conn.execute(
        """
        SELECT drop_active
        FROM settings
        WHERE chat_id=?
        """,
        (chat_id,)
    ).fetchone()

    conn.close()

    return bool(row and row[0] == 1)


# =========================================================
# USER KEYBOARD
# =========================================================

def user_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Drop Name"),
                KeyboardButton(text="📋 Name List")
            ],
            [
                KeyboardButton(text="📊 Poll Status")
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


# =========================================================
# ADMIN KEYBOARD
# =========================================================

def admin_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🟢 Start Drop"),
                KeyboardButton(text="🔴 Stop Drop")
            ],
            [
                KeyboardButton(text="📋 Name List"),
                KeyboardButton(text="🎯 Create Poll")
            ],
            [
                KeyboardButton(text="📊 Poll Status"),
                KeyboardButton(text="🗑 Clear Names")
            ],
            [
                KeyboardButton(text="👥 Total Names"),
                KeyboardButton(text="❌ Close Menu")
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


# =========================================================
# POLL KEYBOARD
# =========================================================

def build_poll_keyboard(poll_id):

    conn = db()

    options = conn.execute(
        """
        SELECT option_id,name,votes
        FROM poll_options
        WHERE poll_id=?
        ORDER BY option_id
        """,
        (poll_id,)
    ).fetchall()

    conn.close()

    buttons = []

    for option_id, name, votes in options:

        buttons.append([
            InlineKeyboardButton(
                text=f"👤 {name}  •  {votes}",
                callback_data=f"vote:{poll_id}:{option_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="📊 Refresh Results",
            callback_data=f"refresh:{poll_id}"
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            text="🛑 Close Poll",
            callback_data=f"close:{poll_id}"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# =========================================================
# /Menu
# =========================================================

@dp.message(
    F.text.regexp(r"^/menu(?:@\w+)?$")
)
async def menu_command(message: Message):

    await message.answer(
        "🎀 <b>WELCOME</b>\n\n"
        "Neeche keyboard se option select karo 👇",
        reply_markup=user_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# /God
# =========================================================

@dp.message(
    F.text.regexp(r"^/god(?:@\w+)?$")
)
async def god_command(message: Message):

    if message.from_user.id != ADMIN_ID:

        await message.answer(
            "❌ <b>Access Denied</b>\n\n"
            "Sirf bot admin ye menu use kar sakta hai.",
            parse_mode="HTML"
        )

        return

    await message.answer(
        "👑 <b>GOD ADMIN PANEL</b>\n\n"
        "Keyboard se control karo 👇",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# /start
# =========================================================

@dp.message(Command("start"))
async def start_command(message: Message):

    await message.answer(
        "🤖 <b>God Poll Bot</b>\n\n"
        "User menu ke liye:\n"
        "<code>/Menu</code>\n\n"
        "Admin panel ke liye:\n"
        "<code>/God</code>",
        parse_mode="HTML"
    )


# =========================================================
# START DROP
# =========================================================

@dp.message(F.text == "🟢 Start Drop")
async def start_drop(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    set_drop(
        message.chat.id,
        True
    )

    await message.answer(
        "🔥 <b>NAME DROP STARTED</b> 🔥\n\n"
        "Ab sab bande:\n"
        "➕ <b>Drop Name</b> dabao\n"
        "aur apna naam bhejo.\n\n"
        "Ek user ka ek naam rahega.",
        reply_markup=user_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# STOP DROP
# =========================================================

@dp.message(F.text == "🔴 Stop Drop")
async def stop_drop(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    set_drop(
        message.chat.id,
        False
    )

    await message.answer(
        "🔴 <b>NAME DROP CLOSED</b>",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# DROP NAME
# =========================================================

@dp.message(F.text == "➕ Drop Name")
async def drop_name(message: Message):

    if not drop_active(message.chat.id):

        await message.answer(
            "❌ Abhi Name Drop start nahi hua."
        )

        return

    await message.answer(
        "✍️ <b>Apna naam bhejo:</b>\n\n"
        "Example: <code>Arjun</code>",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Apna naam..."
        ),
        parse_mode="HTML"
    )


# =========================================================
# RECEIVE NAME
# =========================================================

@dp.message(F.reply_to_message)
async def receive_name(message: Message):

    if not drop_active(message.chat.id):
        return

    if not message.text:
        return

    reply = message.reply_to_message

    if not reply or not reply.from_user:
        return

    try:

        me = await bot.get_me()

        if reply.from_user.id != me.id:
            return

    except Exception:

        return

    name = " ".join(
        message.text.strip().split()
    )

    if not name:
        return

    if len(name) > 50:

        await message.answer(
            "❌ Naam 50 characters se chhota rakho."
        )

        return

    result = add_or_update_name(
        message.chat.id,
        message.from_user.id,
        name
    )

    if result == "updated":

        await message.answer(
            f"♻️ <b>Name Updated!</b>\n\n"
            f"👤 {name}",
            parse_mode="HTML"
        )

    else:

        await message.answer(
            f"✅ <b>Name Added!</b>\n\n"
            f"👤 {name}",
            parse_mode="HTML"
        )


# =========================================================
# NAME LIST
# =========================================================

@dp.message(F.text == "📋 Name List")
async def name_list(message: Message):

    rows = get_names(
        message.chat.id
    )

    if not rows:

        await message.answer(
            "📋 <b>Name List Empty</b>",
            parse_mode="HTML"
        )

        return

    text = "📋 <b>NAME DROP LIST</b>\n\n"

    for index, (_, name) in enumerate(rows, 1):

        text += f"{index}. {name}\n"

    text += f"\n👥 <b>Total:</b> {len(rows)}"

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================================================
# TOTAL NAMES
# =========================================================

@dp.message(F.text == "👥 Total Names")
async def total_names(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    rows = get_names(
        message.chat.id
    )

    await message.answer(
        f"👥 <b>Total Names:</b> {len(rows)}",
        parse_mode="HTML"
    )


# =========================================================
# CREATE CUSTOM POLL
# =========================================================

@dp.message(F.text == "🎯 Create Poll")
async def create_poll(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    chat_id = message.chat.id

    rows = get_names(chat_id)

    if len(rows) < 2:

        await message.answer(
            "❌ Poll banane ke liye minimum 2 names chahiye."
        )

        return

    # Unique names
    names = []
    seen = set()

    for _, name in rows:

        key = name.casefold()

        if key not in seen:

            seen.add(key)
            names.append(name)

    # Create poll
    conn = db()

    cursor = conn.execute(
        """
        INSERT INTO polls(chat_id,question,active)
        VALUES(?,?,1)
        """,
        (
            chat_id,
            "🔥 WHO WILL WIN? 🔥"
        )
    )

    poll_id = cursor.lastrowid

    for index, name in enumerate(names):

        conn.execute(
            """
            INSERT INTO poll_options(
                poll_id,
                option_id,
                name,
                votes
            )
            VALUES(?,?,?,0)
            """,
            (
                poll_id,
                index,
                name
            )
        )

    conn.commit()
    conn.close()

    set_drop(
        chat_id,
        False
    )

    sent = await message.answer(
        "🔥 <b>VOTING STARTED</b> 🔥\n\n"
        "👇 Apne favourite naam par tap karke vote karo.\n\n"
        "⚠️ Ek user = ek vote",
        reply_markup=build_poll_keyboard(poll_id),
        parse_mode="HTML"
    )

    conn = db()

    conn.execute(
        """
        UPDATE polls
        SET message_id=?
        WHERE poll_id=?
        """,
        (
            sent.message_id,
            poll_id
        )
    )

    conn.commit()
    conn.close()


# =========================================================
# VOTE
# =========================================================

@dp.callback_query(
    F.data.startswith("vote:")
)
async def vote(callback: CallbackQuery):

    try:

        _, poll_id, option_id = callback.data.split(":")

        poll_id = int(poll_id)
        option_id = int(option_id)

    except Exception:

        await callback.answer(
            "❌ Invalid vote.",
            show_alert=True
        )

        return

    conn = db()

    poll = conn.execute(
        """
        SELECT active
        FROM polls
        WHERE poll_id=?
        """,
        (poll_id,)
    ).fetchone()

    if not poll or poll[0] != 1:

        conn.close()

        await callback.answer(
            "🔴 Poll closed.",
            show_alert=True
        )

        return

    option = conn.execute(
        """
        SELECT name
        FROM poll_options
        WHERE poll_id=? AND option_id=?
        """,
        (poll_id, option_id)
    ).fetchone()

    if not option:

        conn.close()

        await callback.answer(
            "❌ Option not found.",
            show_alert=True
        )

        return

    old_vote = conn.execute(
        """
        SELECT option_id
        FROM poll_votes
        WHERE poll_id=? AND user_id=?
        """,
        (
            poll_id,
            callback.from_user.id
        )
    ).fetchone()

    # Remove old vote
    if old_vote:

        old_option = old_vote[0]

        conn.execute(
            """
            UPDATE poll_options
            SET votes=CASE
                WHEN votes > 0 THEN votes-1
                ELSE 0
            END
            WHERE poll_id=? AND option_id=?
            """,
            (
                poll_id,
                old_option
            )
        )

        conn.execute(
            """
            DELETE FROM poll_votes
            WHERE poll_id=? AND user_id=?
            """,
            (
                poll_id,
                callback.from_user.id
            )
        )

    # Add new vote
    conn.execute(
        """
        UPDATE poll_options
        SET votes=votes+1
        WHERE poll_id=? AND option_id=?
        """,
        (
            poll_id,
            option_id
        )
    )

    conn.execute(
        """
        INSERT INTO poll_votes(
            poll_id,
            user_id,
            option_id
        )
        VALUES(?,?,?)
        """,
        (
            poll_id,
            callback.from_user.id,
            option_id
        )
    )

    conn.commit()
    conn.close()

    # Update buttons
    try:

        await callback.message.edit_reply_markup(
            reply_markup=build_poll_keyboard(
                poll_id
            )
        )

    except Exception:

        pass

    await callback.answer(
        f"✅ Vote: {option[0]}"
    )


# =========================================================
# REFRESH RESULTS
# =========================================================

@dp.callback_query(
    F.data.startswith("refresh:")
)
async def refresh_poll(callback: CallbackQuery):

    try:

        poll_id = int(
            callback.data.split(":")[1]
        )

    except Exception:

        return

    try:

        await callback.message.edit_reply_markup(
            reply_markup=build_poll_keyboard(
                poll_id
            )
        )

        await callback.answer(
            "📊 Results updated!"
        )

    except Exception:

        await callback.answer(
            "❌ Poll unavailable.",
            show_alert=True
        )


# =========================================================
# CLOSE POLL
# =========================================================

@dp.callback_query(
    F.data.startswith("close:")
)
async def close_poll(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "❌ Admin only.",
            show_alert=True
        )

        return

    try:

        poll_id = int(
            callback.data.split(":")[1]
        )

    except Exception:

        return

    conn = db()

    conn.execute(
        """
        UPDATE polls
        SET active=0
        WHERE poll_id=?
        """,
        (poll_id,)
    )

    conn.commit()
    conn.close()

    try:

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:

        pass

    await callback.message.answer(
        "🛑 <b>POLL CLOSED</b>",
        parse_mode="HTML"
    )

    await callback.answer(
        "Poll closed."
    )


# =========================================================
# POLL STATUS
# =========================================================

@dp.message(F.text == "📊 Poll Status")
async def poll_status(message: Message):

    rows = get_names(
        message.chat.id
    )

    state = (
        "🟢 OPEN"
        if drop_active(message.chat.id)
        else "🔴 CLOSED"
    )

    await message.answer(
        "📊 <b>STATUS</b>\n\n"
        f"Name Drop: {state}\n"
        f"👥 Names: {len(rows)}",
        parse_mode="HTML"
    )


# =========================================================
# CLEAR NAMES
# =========================================================

@dp.message(F.text == "🗑 Clear Names")
async def clear_names_handler(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    clear_names(
        message.chat.id
    )

    await message.answer(
        "🗑 <b>All names cleared!</b>",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# CLOSE MENU
# =========================================================

@dp.message(F.text == "❌ Close Menu")
async def close_menu(message: Message):

    await message.answer(
        "Keyboard menu close kar diya.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="📱 Open Menu"
                    )
                ]
            ],
            resize_keyboard=True
        )
    )


# =========================================================
# OPEN MENU
# =========================================================

@dp.message(F.text == "📱 Open Menu")
async def open_menu(message: Message):

    if message.from_user.id == ADMIN_ID:

        await message.answer(
            "👑 Admin Menu",
            reply_markup=admin_keyboard()
        )

    else:

        await message.answer(
            "🎀 User Menu",
            reply_markup=user_keyboard()
        )


# =========================================================
# UNKNOWN COMMAND
# =========================================================

@dp.message(F.text.startswith("/"))
async def unknown_command(message: Message):

    await message.answer(
        "❌ Unknown command.\n\n"
        "User: /Menu\n"
        "Admin: /God"
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    init_db()

    print("====================================")
    print("🚀 GOD POLL BOT STARTING")
    print("====================================")

    me = await bot.get_me()

    print(f"✅ Connected: @{me.username}")
    print(f"🆔 Bot ID: {me.id}")

    # Remove any old webhook
    await bot.delete_webhook(
        drop_pending_updates=True
    )

    print("✅ Webhook removed")
    print("🚀 BOT IS RUNNING")
    print("====================================")

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    asyncio.run(main())
