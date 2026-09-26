import asyncio
import logging
import os
import re
import sqlite3
import base64

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing!")

if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID must be a numeric Telegram ID!")

if not CHANNEL_ID_RAW:
    raise RuntimeError("CHANNEL_ID is missing!")

try:
    CHANNEL_ID = int(CHANNEL_ID_RAW)
except ValueError:
    raise RuntimeError("CHANNEL_ID must be numeric!")

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

BOT_USERNAME = ""


# =========================================================
# DATABASE
# =========================================================

def db():
    return sqlite3.connect(DB_FILE, timeout=30)


def init_db():

    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            drop_active INTEGER DEFAULT 0,
            drop_message_id INTEGER
        )
    """)

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
        CREATE TABLE IF NOT EXISTS polls (
            poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            waiting_for_name INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


# =========================================================
# GENERAL DATABASE HELPERS
# =========================================================

def set_setting(chat_id, drop_active=None, drop_message_id=None):

    conn = db()

    row = conn.execute(
        "SELECT chat_id FROM settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()

    if row:

        if drop_active is not None:
            conn.execute(
                """
                UPDATE settings
                SET drop_active=?
                WHERE chat_id=?
                """,
                (int(drop_active), chat_id)
            )

        if drop_message_id is not None:
            conn.execute(
                """
                UPDATE settings
                SET drop_message_id=?
                WHERE chat_id=?
                """,
                (drop_message_id, chat_id)
            )

    else:

        conn.execute(
            """
            INSERT INTO settings(
                chat_id,
                drop_active,
                drop_message_id
            )
            VALUES(?,?,?)
            """,
            (
                chat_id,
                int(drop_active or 0),
                drop_message_id
            )
        )

    conn.commit()
    conn.close()


def get_setting(chat_id):

    conn = db()

    row = conn.execute(
        """
        SELECT drop_active, drop_message_id
        FROM settings
        WHERE chat_id=?
        """,
        (chat_id,)
    ).fetchone()

    conn.close()

    if not row:
        return False, None

    return bool(row[0]), row[1]


def set_drop_active(chat_id, active):

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


# =========================================================
# NAME DATABASE
# =========================================================

def normalize_name(name):

    return " ".join(
        name.strip().split()
    )


def name_exists(chat_id, name):

    conn = db()

    rows = conn.execute(
        """
        SELECT name
        FROM names
        WHERE chat_id=?
        """,
        (chat_id,)
    ).fetchall()

    conn.close()

    target = name.casefold()

    for row in rows:

        if row[0].casefold() == target:
            return True

    return False


def add_name(chat_id, user_id, name):

    name = normalize_name(name)

    conn = db()

    existing_user = conn.execute(
        """
        SELECT name
        FROM names
        WHERE chat_id=? AND user_id=?
        """,
        (chat_id, user_id)
    ).fetchone()

    if existing_user:

        conn.close()

        return "already", existing_user[0]

    existing_name = conn.execute(
        """
        SELECT name
        FROM names
        WHERE chat_id=?
        """,
        (chat_id,)
    ).fetchall()

    for row in existing_name:

        if row[0].casefold() == name.casefold():

            conn.close()

            return "taken", row[0]

    conn.execute(
        """
        INSERT INTO names(
            chat_id,
            user_id,
            name
        )
        VALUES(?,?,?)
        """,
        (
            chat_id,
            user_id,
            name
        )
    )

    conn.commit()
    conn.close()

    return "added", name


def get_names(chat_id):

    conn = db()

    rows = conn.execute(
        """
        SELECT user_id,name
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
# USER SESSION
# =========================================================

def set_user_session(
    user_id,
    chat_id,
    waiting_for_name=False
):

    conn = db()

    conn.execute(
        """
        INSERT INTO user_sessions(
            user_id,
            chat_id,
            waiting_for_name
        )
        VALUES(?,?,?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            chat_id=excluded.chat_id,
            waiting_for_name=excluded.waiting_for_name
        """,
        (
            user_id,
            chat_id,
            int(waiting_for_name)
        )
    )

    conn.commit()
    conn.close()


def get_user_session(user_id):

    conn = db()

    row = conn.execute(
        """
        SELECT chat_id,waiting_for_name
        FROM user_sessions
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    conn.close()

    return row


# =========================================================
# DEEP LINK HELPERS
# =========================================================

def encode_channel_id(channel_id):

    raw = str(channel_id).encode()

    return base64.urlsafe_b64encode(
        raw
    ).decode().rstrip("=")


def decode_channel_id(value):

    try:

        padding = "=" * (
            4 - len(value) % 4
        )

        raw = base64.urlsafe_b64decode(
            value + padding
        )

        return int(raw.decode())

    except Exception:

        return None


def drop_deep_link():

    encoded = encode_channel_id(
        CHANNEL_ID
    )

    return f"https://t.me/{BOT_USERNAME}?start=drop_{encoded}"


# =========================================================
# USER KEYBOARD
# =========================================================

def user_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="➕ Drop Name"
                ),
                KeyboardButton(
                    text="📋 Name List"
                )
            ],
            [
                KeyboardButton(
                    text="📊 Poll Status"
                )
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


# =========================================================
# ADMIN PRIVATE KEYBOARD
# =========================================================

def admin_private_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🟢 Start Drop"
                ),
                KeyboardButton(
                    text="🔴 Stop Drop"
                )
            ],
            [
                KeyboardButton(
                    text="📋 Name List"
                ),
                KeyboardButton(
                    text="🎯 Create Poll"
                )
            ],
            [
                KeyboardButton(
                    text="📊 Poll Status"
                ),
                KeyboardButton(
                    text="🗑 Clear Names"
                )
            ],
            [
                KeyboardButton(
                    text="🛑 Close Poll"
                )
            ]
        ],
        resize_keyboard=True,
        is_persistent=True
    )


# =========================================================
# ADMIN CHANNEL INLINE PANEL
# =========================================================

def admin_channel_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="🟢 Start Drop",
                    callback_data="admin:start_drop"
                ),
                InlineKeyboardButton(
                    text="🔴 Stop Drop",
                    callback_data="admin:stop_drop"
                )
            ],

            [
                InlineKeyboardButton(
                    text="📋 Name List",
                    callback_data="admin:name_list"
                ),
                InlineKeyboardButton(
                    text="🎯 Create Poll",
                    callback_data="admin:create_poll"
                )
            ],

            [
                InlineKeyboardButton(
                    text="📊 Poll Status",
                    callback_data="admin:status"
                ),
                InlineKeyboardButton(
                    text="🗑 Clear Names",
                    callback_data="admin:clear_names"
                )
            ],

            [
                InlineKeyboardButton(
                    text="🛑 Close Poll",
                    callback_data="admin:close_poll"
                )
            ]
        ]
    )


# =========================================================
# NAME DROP CHANNEL KEYBOARD
# =========================================================

def name_drop_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="➕ DROP NAME",
                    url=drop_deep_link()
                )
            ]

        ]
    )


# =========================================================
# NAME DROP MESSAGE
# =========================================================

def build_drop_text(active=True):

    names = get_names(CHANNEL_ID)

    if active:

        return (
            "╭━━━━━━━━━━━━━━━━━━╮\n"
            "       📝 <b>NAME DROP</b>\n"
            "╰━━━━━━━━━━━━━━━━━━╯\n\n"
            "Apna naam add karne ke liye\n"
            "neeche button dabao 👇\n\n"
            f"👥 <b>Total Names:</b> {len(names)}"
        )

    return (
        "╭━━━━━━━━━━━━━━━━━━╮\n"
        "       🔴 <b>NAME DROP CLOSED</b>\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👥 <b>Total Names:</b> {len(names)}\n\n"
        "Voting ke liye admin poll create kare."
    )


# =========================================================
# POLL TEXT
# =========================================================

def get_poll_data(poll_id):

    conn = db()

    poll = conn.execute(
        """
        SELECT chat_id,message_id,active
        FROM polls
        WHERE poll_id=?
        """,
        (poll_id,)
    ).fetchone()

    options = conn.execute(
        """
        SELECT option_id,name,votes
        FROM poll_options
        WHERE poll_id=?
        ORDER BY option_id ASC
        """,
        (poll_id,)
    ).fetchall()

    total_votes = conn.execute(
        """
        SELECT COUNT(*)
        FROM poll_votes
        WHERE poll_id=?
        """,
        (poll_id,)
    ).fetchone()[0]

    conn.close()

    return poll, options, total_votes


def build_poll_text(poll_id):

    poll, options, total_votes = get_poll_data(
        poll_id
    )

    if not poll:
        return "❌ Poll not found."

    active = bool(poll[2])

    if active:
        title = "🔥 <b>VOTING STARTED</b> 🔥"
        footer = "👇 Apne favourite naam par tap karo."
    else:
        title = "🛑 <b>POLL CLOSED</b>"
        footer = "Voting ab band hai."

    text = (
        f"{title}\n\n"
        "╭━━━━━━━━━━━━━━━━━━╮\n"
        "        🗳 <b>VOTE</b>\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n\n"
    )

    for index, (_, name, votes) in enumerate(
        options,
        1
    ):

        text += (
            f"{index}. 👤 <b>{name}</b> — "
            f"<b>{votes}</b>\n"
        )

    text += (
        f"\n👥 <b>Total Votes:</b> {total_votes}\n\n"
        f"{footer}"
    )

    return text


def build_poll_keyboard(poll_id):

    poll, options, total_votes = get_poll_data(
        poll_id
    )

    if not poll:
        return InlineKeyboardMarkup(
            inline_keyboard=[]
        )

    active = bool(poll[2])

    buttons = []

    if active:

        for option_id, name, votes in options:

            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"👤 {name} • {votes}",
                        callback_data=(
                            f"vote:{poll_id}:{option_id}"
                        )
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    text="📊 Refresh",
                    callback_data=f"refresh:{poll_id}"
                )
            ]
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text="🛑 Close Poll",
                    callback_data=f"close:{poll_id}"
                )
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):

    return user_id == ADMIN_ID


# =========================================================
# /START
# =========================================================

@dp.message(
    F.chat.type == "private",
    F.text.startswith("/start")
)
async def start_handler(message: Message):

    user_id = message.from_user.id

    parts = message.text.strip().split(
        maxsplit=1
    )

    payload = ""

    if len(parts) == 2:
        payload = parts[1].strip()

    # -----------------------------------------------------
    # Deep link from channel
    # -----------------------------------------------------

    if payload.startswith("drop_"):

        encoded = payload[5:]

        channel_id = decode_channel_id(
            encoded
        )

        if channel_id == CHANNEL_ID:

            set_user_session(
                user_id,
                CHANNEL_ID,
                False
            )

    # -----------------------------------------------------
    # ADMIN
    # -----------------------------------------------------

    if is_admin(user_id):

        await message.answer(
            "👑 <b>GOD×POLL ADMIN PANEL</b>\n\n"
            "Neeche keyboard se control karo 👇",
            reply_markup=admin_private_keyboard(),
            parse_mode="HTML"
        )

        return

    # -----------------------------------------------------
    # USER
    # -----------------------------------------------------

    session = get_user_session(
        user_id
    )

    if not session:

        set_user_session(
            user_id,
            CHANNEL_ID,
            False
        )

    await message.answer(
        "🎀 <b>GOD×POLL</b>\n\n"
        "👋 Welcome!\n\n"
        "Neeche keyboard se option select karo 👇",
        reply_markup=user_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# /GOD PRIVATE
# =========================================================

@dp.message(
    F.chat.type == "private",
    F.text.regexp(r"^/[Gg][Oo][Dd](?:@\w+)?$")
)
async def god_private(message: Message):

    if not is_admin(
        message.from_user.id
    ):

        await message.answer(
            "❌ <b>Access Denied</b>",
            parse_mode="HTML"
        )

        return

    await message.answer(
        "👑 <b>GOD×POLL ADMIN PANEL</b>\n\n"
        "Neeche keyboard se control karo 👇",
        reply_markup=admin_private_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# /GOD CHANNEL
# =========================================================

@dp.channel_post(
    F.text.regexp(r"^/[Gg][Oo][Dd](?:@\w+)?$")
)
async def god_channel(message: Message):

    # Channel commands don't have from_user like normal messages.
    # Only the configured channel is accepted.
    if message.chat.id != CHANNEL_ID:
        return

    await message.answer(
        "👑 <b>GOD×POLL ADMIN PANEL</b>\n\n"
        "Admin buttons neeche hain 👇",
        reply_markup=admin_channel_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# START DROP FUNCTION
# =========================================================

async def start_drop():

    # Clear previous round
    clear_names(CHANNEL_ID)

    # Stop any active old poll
    conn = db()

    conn.execute(
        """
        UPDATE polls
        SET active=0
        WHERE chat_id=? AND active=1
        """,
        (CHANNEL_ID,)
    )

    conn.commit()
    conn.close()

    set_drop_active(
        CHANNEL_ID,
        True
    )

    sent = await bot.send_message(
        CHANNEL_ID,
        build_drop_text(True),
        reply_markup=name_drop_keyboard(),
        parse_mode="HTML"
    )

    set_setting(
        CHANNEL_ID,
        drop_message_id=sent.message_id
    )

    return sent


# =========================================================
# UPDATE DROP BOX
# =========================================================

async def update_drop_box():

    active, message_id = get_setting(
        CHANNEL_ID
    )

    if not message_id:
        return

    try:

        await bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=message_id,
            text=build_drop_text(active),
            reply_markup=(
                name_drop_keyboard()
                if active
                else None
            ),
            parse_mode="HTML"
        )

    except TelegramBadRequest:
        pass


# =========================================================
# STOP DROP
# =========================================================

async def stop_drop():

    set_drop_active(
        CHANNEL_ID,
        False
    )

    await update_drop_box()


# =========================================================
# CREATE POLL
# =========================================================

async def create_poll():

    names = get_names(
        CHANNEL_ID
    )

    if len(names) < 2:

        return None, (
            "❌ Poll banane ke liye "
            "minimum <b>2 names</b> chahiye."
        )

    # Safety: unique display names
    unique_names = []
    seen = set()

    for _, name in names:

        key = name.casefold()

        if key not in seen:

            seen.add(key)
            unique_names.append(name)

    if len(unique_names) < 2:

        return None, (
            "❌ Minimum 2 different names "
            "chahiye."
        )

    conn = db()

    cursor = conn.execute(
        """
        INSERT INTO polls(
            chat_id,
            active
        )
        VALUES(?,1)
        """,
        (CHANNEL_ID,)
    )

    poll_id = cursor.lastrowid

    for index, name in enumerate(
        unique_names
    ):

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

    # Name drop automatically closes
    await stop_drop()

    sent = await bot.send_message(
        CHANNEL_ID,
        build_poll_text(poll_id),
        reply_markup=build_poll_keyboard(
            poll_id
        ),
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

    return poll_id, None


# =========================================================
# ADMIN PRIVATE BUTTONS
# =========================================================

@dp.message(
    F.chat.type == "private",
    F.text == "🟢 Start Drop"
)
async def private_start_drop(message: Message):

    if not is_admin(
        message.from_user.id
    ):
        return

    await start_drop()

    await message.answer(
        "🟢 <b>NAME DROP STARTED</b>\n\n"
        "Channel me Name Drop box create ho gaya.",
        reply_markup=admin_private_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    F.chat.type == "private",
    F.text == "🔴 Stop Drop"
)
async def private_stop_drop(message: Message):

    if not is_admin(
        message.from_user.id
    ):
        return

    await stop_drop()

    await message.answer(
        "🔴 <b>NAME DROP CLOSED</b>",
        reply_markup=admin_private_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    F.chat.type == "private",
    F.text == "📋 Name List"
)
async def private_name_list(message: Message):

    rows = get_names(
        CHANNEL_ID
    )

    if not rows:

        await message.answer(
            "📋 <b>Name List Empty</b>",
            parse_mode="HTML"
        )

        return

    text = "📋 <b>NAME DROP LIST</b>\n\n"

    for index, (_, name) in enumerate(
        rows,
        1
    ):

        text += f"{index}. {name}\n"

    text += (
        f"\n👥 <b>Total:</b> {len(rows)}"
    )

    await message.answer(
        text,
        parse_mode="HTML"
    )


@dp.message(
    F.chat.type == "private",
    F.text == "🎯 Create Poll"
)
async def private_create_poll(message: Message):

    if not is_admin(
        message.from_user.id
    ):
        return

    poll_id, error = await create_poll()

    if error:

        await message.answer(
            error,
            parse_mode="HTML"
        )

        return

    await message.answer(
        "🔥 <b>POLL CREATED</b>\n\n"
        "Voting box channel me post ho gaya.",
        reply_markup=admin_private_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    F.chat.type == "private",
    F.text == "📊 Poll Status"
)
async def private_poll_status(message: Message):

    active_drop, _ = get_setting(
        CHANNEL_ID
    )

    conn = db()

    active_poll = conn.execute(
        """
        SELECT poll_id
        FROM polls
        WHERE chat_id=? AND active=1
        ORDER BY poll_id DESC
        LIMIT 1
        """,
        (CHANNEL_ID,)
    ).fetchone()

    conn.close()

    names = get_names(
        CHANNEL_ID
    )

    text = (
        "📊 <b>GOD×POLL STATUS</b>\n\n"
        f"📝 Name Drop: "
        f"{'🟢 OPEN' if active_drop else '🔴 CLOSED'}\n"
        f"👥 Names: {len(names)}\n"
    )

    if active_poll:

        _, _, total_votes = get_poll_data(
            active_poll[0]
        )

        text += (
            "\n🗳 Poll: 🟢 ACTIVE\n"
            f"👥 Total Votes: {total_votes}"
        )

    else:

        text += "\n🗳 Poll: 🔴 NONE"

    await message.answer(
        text,
        parse_mode="HTML"
    )


@dp.message(
    F.chat.type == "private",
    F.text == "🗑 Clear Names"
)
async def private_clear_names(message: Message):

    if not is_admin(
        message.from_user.id
    ):
        return

    clear_names(
        CHANNEL_ID
    )

    await update_drop_box()

    await message.answer(
        "🗑 <b>All names cleared!</b>",
        reply_markup=admin_private_keyboard(),
        parse_mode="HTML"
    )


@dp.message(
    F.chat.type == "private",
    F.text == "🛑 Close Poll"
)
async def private_close_poll(message: Message):

    if not is_admin(
        message.from_user.id
    ):
        return

    poll_id = get_latest_active_poll()

    if not poll_id:

        await message.answer(
            "❌ Koi active poll nahi hai."
        )

        return

    await close_poll_by_id(
        poll_id
    )

    await message.answer(
        "🛑 <b>Poll closed.</b>",
        reply_markup=admin_private_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# USER DROP NAME
# =========================================================

@dp.message(
    F.chat.type == "private",
    F.text == "➕ Drop Name"
)
async def user_drop_name(message: Message):

    session = get_user_session(
        message.from_user.id
    )

    if not session:

        set_user_session(
            message.from_user.id,
            CHANNEL_ID,
            False
        )

        chat_id = CHANNEL_ID

    else:

        chat_id = session[0]

    active, _ = get_setting(
        chat_id
    )

    if not active:

        await message.answer(
            "🔴 <b>Name Drop abhi closed hai.</b>\n\n"
            "Admin ke Start Drop karne ke baad "
            "dobara try karo.",
            parse_mode="HTML"
        )

        return

    set_user_session(
        message.from_user.id,
        chat_id,
        True
    )

    await message.answer(
        "✍️ <b>Apna naam bhejo</b>\n\n"
        "Example:\n"
        "<code>Arjun</code>\n\n"
        "⚠️ Ek user sirf ek naam drop kar sakta hai.",
        parse_mode="HTML"
    )


# =========================================================
# RECEIVE USER NAME
# =========================================================

@dp.message(
    F.chat.type == "private",
    F.text
)
async def receive_private_text(message: Message):

    # Commands are handled separately
    if message.text.startswith("/"):
        return

    session = get_user_session(
        message.from_user.id
    )

    if not session:
        return

    chat_id, waiting = session

    if not waiting:
        return

    active, _ = get_setting(
        chat_id
    )

    if not active:

        set_user_session(
            message.from_user.id,
            chat_id,
            False
        )

        await message.answer(
            "🔴 Name Drop close ho chuka hai.",
            reply_markup=user_keyboard()
        )

        return

    name = normalize_name(
        message.text
    )

    if not name:
        return

    if len(name) > 50:

        await message.answer(
            "❌ Naam maximum 50 characters ka ho."
        )

        return

    result, value = add_name(
        chat_id,
        message.from_user.id,
        name
    )

    if result == "already":

        set_user_session(
            message.from_user.id,
            chat_id,
            False
        )

        await message.answer(
            "⚠️ <b>Aap already name drop kar chuke ho.</b>\n\n"
            f"👤 Current Name: <b>{value}</b>",
            reply_markup=user_keyboard(),
            parse_mode="HTML"
        )

        return

    if result == "taken":

        await message.answer(
            "⚠️ Ye naam already kisi aur ne "
            "drop kiya hua hai.\n\n"
            "Koi different naam bhejo.",
            parse_mode="HTML"
        )

        return

    set_user_session(
        message.from_user.id,
        chat_id,
        False
    )

    await update_drop_box()

    await message.answer(
        "✅ <b>Name Added Successfully!</b>\n\n"
        f"👤 <b>{name}</b>\n\n"
        "Aapka naam channel ke Name Drop me "
        "add ho gaya.",
        reply_markup=user_keyboard(),
        parse_mode="HTML"
    )


# =========================================================
# USER NAME LIST
# =========================================================

@dp.message(
    F.chat.type == "private",
    F.text == "📋 Name List"
)
async def user_name_list(message: Message):

    rows = get_names(
        CHANNEL_ID
    )

    if not rows:

        await message.answer(
            "📋 Abhi koi naam drop nahi hua."
        )

        return

    text = "📋 <b>CURRENT NAME LIST</b>\n\n"

    for index, (_, name) in enumerate(
        rows,
        1
    ):

        text += (
            f"{index}. {name}\n"
        )

    text += (
        f"\n👥 <b>Total:</b> {len(rows)}"
    )

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================================================
# USER POLL STATUS
# =========================================================

@dp.message(
    F.chat.type == "private",
    F.text == "📊 Poll Status"
)
async def user_poll_status(message: Message):

    active_drop, _ = get_setting(
        CHANNEL_ID
    )

    poll_id = get_latest_active_poll()

    names = get_names(
        CHANNEL_ID
    )

    text = (
        "📊 <b>STATUS</b>\n\n"
        f"📝 Name Drop: "
        f"{'🟢 OPEN' if active_drop else '🔴 CLOSED'}\n"
        f"👥 Names: {len(names)}\n"
    )

    if poll_id:

        _, _, total_votes = get_poll_data(
            poll_id
        )

        text += (
            "\n🗳 Voting: 🟢 ACTIVE\n"
            f"👥 Votes: {total_votes}"
        )

    else:

        text += "\n🗳 Voting: 🔴 CLOSED / NONE"

    await message.answer(
        text,
        parse_mode="HTML"
    )


# =========================================================
# ADMIN CHANNEL CALLBACK
# =========================================================

@dp.callback_query(
    F.data.startswith("admin:")
)
async def admin_callback(
    callback: CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "❌ Admin only.",
            show_alert=True
        )

        return

    action = callback.data.split(
        ":",
        1
    )[1]

    # -----------------------------------------------------
    # START DROP
    # -----------------------------------------------------

    if action == "start_drop":

        await start_drop()

        await callback.answer(
            "🟢 Name Drop started!"
        )

        return

    # -----------------------------------------------------
    # STOP DROP
    # -----------------------------------------------------

    if action == "stop_drop":

        await stop_drop()

        await callback.answer(
            "🔴 Name Drop stopped!"
        )

        return

    # -----------------------------------------------------
    # NAME LIST
    # -----------------------------------------------------

    if action == "name_list":

        rows = get_names(
            CHANNEL_ID
        )

        if not rows:

            text = "📋 <b>NAME LIST EMPTY</b>"

        else:

            text = "📋 <b>NAME LIST</b>\n\n"

            for index, (_, name) in enumerate(
                rows,
                1
            ):

                text += (
                    f"{index}. {name}\n"
                )

            text += (
                f"\n👥 <b>Total:</b> {len(rows)}"
            )

        await callback.message.answer(
            text,
            parse_mode="HTML"
        )

        await callback.answer()

        return

    # -----------------------------------------------------
    # CREATE POLL
    # -----------------------------------------------------

    if action == "create_poll":

        poll_id, error = await create_poll()

        if error:

            await callback.answer(
                "❌ Minimum 2 names required.",
                show_alert=True
            )

            return

        await callback.answer(
            "🔥 Poll created!"
        )

        return

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    if action == "status":

        active_drop, _ = get_setting(
            CHANNEL_ID
        )

        names = get_names(
            CHANNEL_ID
        )

        poll_id = get_latest_active_poll()

        text = (
            "📊 <b>STATUS</b>\n\n"
            f"📝 Name Drop: "
            f"{'🟢 OPEN' if active_drop else '🔴 CLOSED'}\n"
            f"👥 Names: {len(names)}\n"
        )

        if poll_id:

            _, _, total_votes = get_poll_data(
                poll_id
            )

            text += (
                "\n🗳 Poll: 🟢 ACTIVE\n"
                f"👥 Votes: {total_votes}"
            )

        else:

            text += "\n🗳 Poll: 🔴 NONE"

        await callback.message.answer(
            text,
            parse_mode="HTML"
        )

        await callback.answer()

        return

    # -----------------------------------------------------
    # CLEAR NAMES
    # -----------------------------------------------------

    if action == "clear_names":

        clear_names(
            CHANNEL_ID
        )

        await update_drop_box()

        await callback.answer(
            "🗑 Names cleared!"
        )

        return

    # -----------------------------------------------------
    # CLOSE POLL
    # -----------------------------------------------------

    if action == "close_poll":

        poll_id = get_latest_active_poll()

        if not poll_id:

            await callback.answer(
                "❌ No active poll.",
                show_alert=True
            )

            return

        await close_poll_by_id(
            poll_id
        )

        await callback.answer(
            "🛑 Poll closed!"
        )

        return


# =========================================================
# LATEST ACTIVE POLL
# =========================================================

def get_latest_active_poll():

    conn = db()

    row = conn.execute(
        """
        SELECT poll_id
        FROM polls
        WHERE chat_id=? AND active=1
        ORDER BY poll_id DESC
        LIMIT 1
        """,
        (CHANNEL_ID,)
    ).fetchone()

    conn.close()

    if not row:
        return None

    return row[0]


# =========================================================
# VOTE
# =========================================================

@dp.callback_query(
    F.data.startswith("vote:")
)
async def vote_callback(
    callback: CallbackQuery
):

    try:

        _, poll_id_raw, option_id_raw = (
            callback.data.split(":")
        )

        poll_id = int(
            poll_id_raw
        )

        option_id = int(
            option_id_raw
        )

    except Exception:

        await callback.answer(
            "❌ Invalid vote.",
            show_alert=True
        )

        return

    conn = db()

    try:

        # Check poll
        poll = conn.execute(
            """
            SELECT active
            FROM polls
            WHERE poll_id=?
            """,
            (poll_id,)
        ).fetchone()

        if not poll:

            await callback.answer(
                "❌ Poll not found.",
                show_alert=True
            )

            return

        if poll[0] != 1:

            await callback.answer(
                "🔴 Poll closed.",
                show_alert=True
            )

            return

        # Check option
        option = conn.execute(
            """
            SELECT name
            FROM poll_options
            WHERE poll_id=? AND option_id=?
            """,
            (
                poll_id,
                option_id
            )
        ).fetchone()

        if not option:

            await callback.answer(
                "❌ Option not found.",
                show_alert=True
            )

            return

        # Start transaction
        conn.execute(
            "BEGIN IMMEDIATE"
        )

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

        # Same option clicked again
        if old_vote and old_vote[0] == option_id:

            conn.rollback()

            await callback.answer(
                f"✅ Aapka vote already {option[0]} ko hai."
            )

            return

        # Remove previous vote
        if old_vote:

            old_option_id = old_vote[0]

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
                    old_option_id
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

    except Exception as e:

        conn.rollback()

        logging.exception(
            "Vote error: %s",
            e
        )

        await callback.answer(
            "❌ Vote process error.",
            show_alert=True
        )

        return

    finally:

        conn.close()

    # Update channel poll
    try:

        await callback.message.edit_text(
            build_poll_text(
                poll_id
            ),
            reply_markup=build_poll_keyboard(
                poll_id
            ),
            parse_mode="HTML"
        )

    except TelegramBadRequest as e:

        logging.warning(
            "Poll message update warning: %s",
            e
        )

    await callback.answer(
        f"✅ Vote added: {option[0]}"
    )


# =========================================================
# REFRESH POLL
# =========================================================

@dp.callback_query(
    F.data.startswith("refresh:")
)
async def refresh_callback(
    callback: CallbackQuery
):

    try:

        poll_id = int(
            callback.data.split(":")[1]
        )

    except Exception:

        await callback.answer(
            "❌ Invalid poll.",
            show_alert=True
        )

        return

    await callback.message.edit_text(
        build_poll_text(
            poll_id
        ),
        reply_markup=build_poll_keyboard(
            poll_id
        ),
        parse_mode="HTML"
    )

    await callback.answer(
        "📊 Results refreshed!"
    )


# =========================================================
# CLOSE POLL FUNCTION
# =========================================================

async def close_poll_by_id(
    poll_id
):

    conn = db()

    row = conn.execute(
        """
        SELECT chat_id,message_id
        FROM polls
        WHERE poll_id=?
        """,
        (poll_id,)
    ).fetchone()

    if not row:

        conn.close()
        return

    chat_id, message_id = row

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

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=build_poll_text(
                poll_id
            ),
            reply_markup=None,
            parse_mode="HTML"
        )

    except TelegramBadRequest as e:

        logging.warning(
            "Close poll edit warning: %s",
            e
        )


# =========================================================
# CLOSE POLL CALLBACK
# =========================================================

@dp.callback_query(
    F.data.startswith("close:")
)
async def close_callback(
    callback: CallbackQuery
):

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

        await callback.answer(
            "❌ Invalid poll.",
            show_alert=True
        )

        return

    await close_poll_by_id(
        poll_id
    )

    await callback.answer(
        "🛑 Poll closed!"
    )


# =========================================================
# UNKNOWN COMMAND
# =========================================================

@dp.message(
    F.chat.type == "private",
    F.text.startswith("/")
)
async def unknown_command(
    message: Message
):

    await message.answer(
        "❌ Unknown command.\n\n"
        "User ke liye sirf <b>/start</b> use karo.",
        parse_mode="HTML"
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    global BOT_USERNAME

    init_db()

    print(
        "===================================="
    )
    print(
        "🚀 GOD×POLL BOT STARTING"
    )
    print(
        "===================================="
    )

    me = await bot.get_me()

    BOT_USERNAME = me.username or ""

    print(
        f"✅ Connected: @{BOT_USERNAME}"
    )

    print(
        f"🆔 Bot ID: {me.id}"
    )

    print(
        f"👑 Admin ID: {ADMIN_ID}"
    )

    print(
        f"📢 Channel ID: {CHANNEL_ID}"
    )

    # Remove old webhook
    await bot.delete_webhook(
        drop_pending_updates=True
    )

    print(
        "✅ Webhook removed"
    )

    print(
        "🚀 BOT IS RUNNING"
    )

    print(
        "===================================="
    )

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "channel_post"
        ]
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "🛑 Bot stopped."
        )
