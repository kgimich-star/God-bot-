import asyncio
import html
import logging
import os
import sqlite3
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.deep_linking import create_start_link


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it to Railway/GitHub Variables.")

if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID must be your numeric Telegram user ID.")

ADMIN_ID = int(ADMIN_ID_RAW)
DB_FILE = os.getenv("DB_FILE", "god_poll.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()


# ============================================================
# DATABASE
# ============================================================

def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    with closing(db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                title TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                drop_active INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, user_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS poll_options (
                poll_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                votes INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (poll_id, option_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS poll_votes (
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                PRIMARY KEY (poll_id, user_id)
            )
        """)

        conn.commit()


def get_channel():
    with closing(db()) as conn:
        return conn.execute("""
            SELECT chat_id, username, title
            FROM channels
            WHERE admin_id=?
            ORDER BY id DESC
            LIMIT 1
        """, (ADMIN_ID,)).fetchone()


def save_channel(chat_id, username, title):
    with closing(db()) as conn:
        conn.execute("""
            INSERT INTO channels(admin_id, chat_id, username, title)
            VALUES(?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                admin_id=excluded.admin_id,
                username=excluded.username,
                title=excluded.title
        """, (ADMIN_ID, chat_id, username, title))

        conn.execute("""
            INSERT INTO settings(chat_id, drop_active)
            VALUES(?,0)
            ON CONFLICT(chat_id) DO NOTHING
        """, (chat_id,))
        conn.commit()


def set_drop(chat_id, active):
    with closing(db()) as conn:
        conn.execute("""
            INSERT INTO settings(chat_id, drop_active)
            VALUES(?,?)
            ON CONFLICT(chat_id)
            DO UPDATE SET drop_active=excluded.drop_active
        """, (chat_id, int(active)))
        conn.commit()


def is_drop_active(chat_id):
    with closing(db()) as conn:
        row = conn.execute("""
            SELECT drop_active FROM settings WHERE chat_id=?
        """, (chat_id,)).fetchone()
        return bool(row and row[0])


def add_or_update_name(chat_id, user_id, name):
    with closing(db()) as conn:
        old = conn.execute("""
            SELECT name FROM names
            WHERE chat_id=? AND user_id=?
        """, (chat_id, user_id)).fetchone()

        if old:
            conn.execute("""
                UPDATE names SET name=?
                WHERE chat_id=? AND user_id=?
            """, (name, chat_id, user_id))
            result = "updated"
        else:
            conn.execute("""
                INSERT INTO names(chat_id,user_id,name)
                VALUES(?,?,?)
            """, (chat_id, user_id, name))
            result = "added"

        conn.commit()
        return result


def get_names(chat_id):
    with closing(db()) as conn:
        return conn.execute("""
            SELECT user_id, name
            FROM names
            WHERE chat_id=?
            ORDER BY id ASC
        """, (chat_id,)).fetchall()


def clear_names(chat_id):
    with closing(db()) as conn:
        conn.execute("DELETE FROM names WHERE chat_id=?", (chat_id,))
        conn.commit()


def create_poll_db(chat_id, names):
    with closing(db()) as conn:
        cur = conn.execute("""
            INSERT INTO polls(chat_id, active)
            VALUES(?,1)
        """, (chat_id,))
        poll_id = cur.lastrowid

        for option_id, name in enumerate(names):
            conn.execute("""
                INSERT INTO poll_options(poll_id,option_id,name,votes)
                VALUES(?,?,?,0)
            """, (poll_id, option_id, name))

        conn.commit()
        return poll_id


def set_poll_message_id(poll_id, message_id):
    with closing(db()) as conn:
        conn.execute("""
            UPDATE polls SET message_id=? WHERE poll_id=?
        """, (message_id, poll_id))
        conn.commit()


def get_poll(poll_id):
    with closing(db()) as conn:
        return conn.execute("""
            SELECT chat_id, message_id, active
            FROM polls WHERE poll_id=?
        """, (poll_id,)).fetchone()


def get_poll_options(poll_id):
    with closing(db()) as conn:
        return conn.execute("""
            SELECT option_id,name,votes
            FROM poll_options
            WHERE poll_id=?
            ORDER BY option_id
        """, (poll_id,)).fetchall()


def close_poll_db(poll_id):
    with closing(db()) as conn:
        conn.execute("""
            UPDATE polls SET active=0 WHERE poll_id=?
        """, (poll_id,))
        conn.commit()


def get_active_poll(chat_id):
    with closing(db()) as conn:
        return conn.execute("""
            SELECT poll_id,message_id
            FROM polls
            WHERE chat_id=? AND active=1
            ORDER BY poll_id DESC
            LIMIT 1
        """, (chat_id,)).fetchone()


# ============================================================
# KEYBOARDS
# ============================================================

def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 Channel")],
            [KeyboardButton(text="➕ Drop Name"), KeyboardButton(text="🛑 Stop Drop")],
            [KeyboardButton(text="📋 Name List"), KeyboardButton(text="🎯 Create Poll")],
            [KeyboardButton(text="📊 Poll Status"), KeyboardButton(text="🗑 Clear Names")],
            [KeyboardButton(text="❌ Close Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def user_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Drop Name")],
            [KeyboardButton(text="📋 My Name")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def open_bot_keyboard(bot_username):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ DROP NAME",
                    url=f"https://t.me/{bot_username}?start=drop",
                )
            ]
        ]
    )


def poll_keyboard(poll_id):
    rows = get_poll_options(poll_id)
    buttons = []

    for option_id, name, votes in rows:
        # Telegram inline buttons cannot have arbitrary text colours.
        # Red/green status is represented with emoji.
        icon = "🔴" if votes == 0 else "🟢"
        buttons.append([
            InlineKeyboardButton(
                text=f"👤 {name}   {icon} {votes}",
                callback_data=f"vote:{poll_id}:{option_id}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="🔄 Refresh",
            callback_data=f"refresh:{poll_id}",
        ),
        InlineKeyboardButton(
            text="🛑 Close Poll",
            callback_data=f"close:{poll_id}",
        ),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ============================================================
# FSM STATES
# ============================================================

class SetupState(StatesGroup):
    waiting_channel = State()


class NameState(StatesGroup):
    waiting_name = State()


# ============================================================
# HELPERS
# ============================================================

async def admin_only(message: Message):
    if message.from_user and message.from_user.id == ADMIN_ID:
        return True

    await message.answer("❌ Admin only.")
    return False


async def check_channel_admin(chat_id):
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        return member.status in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
    except Exception:
        return False


# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    # User menu is intentionally private-chat only.
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "🤖 Mujhe private chat me open karo aur /start dabao."
        )
        return

    payload = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip().lower()

    channel = get_channel()

    if payload == "drop":
        if not channel:
            await message.answer(
                "❌ Abhi koi channel configured nahi hai."
            )
            return

        chat_id = channel[0]

        if not is_drop_active(chat_id):
            await message.answer(
                "🔴 <b>Name Drop abhi closed hai.</b>"
            )
            return

        await message.answer(
            "🔥 <b>NAME DROP OPEN</b> 🔥\n\n"
            "Neeche <b>➕ Drop Name</b> dabao aur apna naam bhejo.",
            reply_markup=user_keyboard(),
        )
        return

    await message.answer(
        "🎀 <b>🐣🎀𝐇ᴇ𝐋ʟᴏ 𝐌ᴇ𝐑ᴇ 𝐊ᴜᴄʜᴜ 𝐏ᴜᴄʜᴜ ♡🎀🥰 𝐒ᴡᴀɢᴀᴛ 𝐇ᴀɪ 𝐀ᴘᴋᴀ 𝐘ᴀʜᴀ 𝐏ᴀʀ🧸💞</b> 🎀\n\n"
        "Neeche keyboard se option select karo 👇",
        reply_markup=user_keyboard(),
    )


# ============================================================
# /GOD
# ============================================================

@dp.message(Command("god"))
async def god_handler(message: Message, state: FSMContext):
    await state.clear()

    if message.chat.type != ChatType.PRIVATE:
        await message.answer("❌ /God sirf bot ki private chat me use karo.")
        return

    if not await admin_only(message):
        return

    channel = get_channel()

    if channel:
        channel_text = (
            f"📢 <b>Channel:</b> "
            f"{html.escape(channel[2])}\n"
            f"🆔 <code>{channel[0]}</code>"
        )
    else:
        channel_text = "📢 <b>Channel:</b> Not configured"

    await message.answer(
        "👑 <b>GOD ADMIN PANEL</b>\n\n"
        f"{channel_text}\n\n"
        "Keyboard se control karo 👇",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# CHANNEL SETUP
# ============================================================

@dp.message(F.text == "📢 Channel")
async def channel_button(message: Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE or not await admin_only(message):
        return

    await state.set_state(SetupState.waiting_channel)

    await message.answer(
        "📢 <b>Channel Setup</b>\n\n"
        "Apna public channel username bhejo.\n\n"
        "Example:\n"
        "<code>@GodPoll</code>\n\n"
        "⚠️ Bot ko us channel me Administrator banana zaroori hai.",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="@YourChannel",
        ),
    )


@dp.message(SetupState.waiting_channel)
async def receive_channel(message: Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return

    if message.from_user.id != ADMIN_ID:
        return

    raw = (message.text or "").strip()

    if not raw.startswith("@") or len(raw) < 2:
        await message.answer(
            "❌ Valid public channel username bhejo.\n"
            "Example: <code>@GodPoll</code>"
        )
        return

    username = raw

    try:
        chat = await bot.get_chat(username)
    except Exception as e:
        logging.warning("Channel lookup failed: %s", e)
        await message.answer(
            "❌ Channel nahi mila.\n\n"
            "Username check karo aur ensure karo ki bot channel me added hai."
        )
        return

    if chat.type != ChatType.CHANNEL:
        await message.answer("❌ Ye Telegram channel nahi hai.")
        return

    if not await check_channel_admin(chat.id):
        await message.answer(
            "❌ Bot ko is channel me Administrator banao.\n\n"
            "Phir dobara 📢 Channel setup karo."
        )
        return

    save_channel(
        chat.id,
        chat.username or username.lstrip("@"),
        chat.title or "Channel",
    )
    await state.clear()

    await message.answer(
        "✅ <b>CHANNEL CONNECTED</b>\n\n"
        f"📢 {html.escape(chat.title or 'Channel')}\n"
        f"🔗 @{html.escape(chat.username or username.lstrip('@'))}\n\n"
        "Ab <b>➕ Drop Name</b> dabao.",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN: START DROP / DROP NAME
# ============================================================

@dp.message(F.text == "➕ Drop Name")
async def admin_drop_name(message: Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE or not await admin_only(message):
        return

    await state.clear()

    channel = get_channel()

    if not channel:
        await message.answer(
            "❌ Pehle 📢 Channel se channel connect karo.",
            reply_markup=admin_keyboard(),
        )
        return

    chat_id, username, title = channel

    if not await check_channel_admin(chat_id):
        await message.answer(
            "❌ Bot abhi channel me Administrator nahi hai."
        )
        return

    set_drop(chat_id, True)

    me = await bot.get_me()

    await bot.send_message(
        chat_id,
        "🔥 <b>NAME DROP STARTED</b> 🔥\n\n"
        "Apna naam drop karne ke liye neeche button dabao 👇\n\n"
        "⚠️ Naam <b>private chat</b> me submit hoga.",
        reply_markup=open_bot_keyboard(me.username),
    )

    await message.answer(
        "✅ <b>Name Drop Started</b>\n\n"
        f"📢 {html.escape(title)}\n\n"
        "Channel me Drop Name message bhej diya gaya.",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# USER: DROP NAME
# ============================================================

@dp.message(F.text == "➕ Drop Name")
async def user_drop_name(message: Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Name private chat me submit hoga.\n"
            "Bot ki private chat open karo."
        )
        return

    channel = get_channel()

    if not channel:
        await message.answer("❌ Abhi channel configured nahi hai.")
        return

    chat_id = channel[0]

    if not is_drop_active(chat_id):
        await message.answer("🔴 <b>Name Drop closed hai.</b>")
        return

    await state.set_state(NameState.waiting_name)

    await message.answer(
        "✍️ <b>Apna naam bhejo:</b>\n\n"
        "Example: <code>godx</code>",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Apna naam...",
        ),
    )


@dp.message(NameState.waiting_name)
async def receive_name(message: Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return

    channel = get_channel()

    if not channel:
        await state.clear()
        await message.answer("❌ Channel configured nahi hai.")
        return

    chat_id = channel[0]

    if not is_drop_active(chat_id):
        await state.clear()
        await message.answer("🔴 Name Drop close ho chuka hai.")
        return

    name = " ".join((message.text or "").strip().split())

    if not name:
        await message.answer("❌ Valid naam bhejo.")
        return

    if len(name) > 50:
        await message.answer("❌ Naam maximum 50 characters ka rakho.")
        return

    result = add_or_update_name(
        chat_id,
        message.from_user.id,
        name,
    )

    await state.clear()

    if result == "updated":
        await message.answer(
            f"♻️ <b>Name Updated!</b>\n\n"
            f"👤 {html.escape(name)}",
            reply_markup=user_keyboard(),
        )
    else:
        await message.answer(
            f"✅ <b>Name Added!</b>\n\n"
            f"👤 {html.escape(name)}",
            reply_markup=user_keyboard(),
        )


# ============================================================
# USER: MY NAME
# ============================================================

@dp.message(F.text == "📋 My Name")
async def my_name(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return

    channel = get_channel()

    if not channel:
        await message.answer("❌ Channel configured nahi hai.")
        return

    rows = get_names(channel[0])
    mine = next(
        (name for user_id, name in rows if user_id == message.from_user.id),
        None,
    )

    if not mine:
        await message.answer(
            "📋 <b>Your name:</b> Not submitted yet."
        )
    else:
        await message.answer(
            f"📋 <b>Your name:</b> {html.escape(mine)}"
        )


# ============================================================
# ADMIN: STOP DROP
# ============================================================

@dp.message(F.text == "🛑 Stop Drop")
async def stop_drop(message: Message):
    if message.chat.type != ChatType.PRIVATE or not await admin_only(message):
        return

    channel = get_channel()

    if not channel:
        await message.answer("❌ Pehle channel connect karo.")
        return

    set_drop(channel[0], False)

    await message.answer(
        "🔴 <b>NAME DROP CLOSED</b>\n\n"
        f"📢 {html.escape(channel[2])}",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN: NAME LIST
# ============================================================

@dp.message(F.text == "📋 Name List")
async def name_list(message: Message):
    if message.chat.type != ChatType.PRIVATE or not await admin_only(message):
        return

    channel = get_channel()

    if not channel:
        await message.answer("❌ Pehle channel connect karo.")
        return

    rows = get_names(channel[0])

    if not rows:
        await message.answer("📋 <b>Name List Empty</b>")
        return

    text = "📋 <b>NAME DROP LIST</b>\n\n"

    for i, (_, name) in enumerate(rows, 1):
        text += f"{i}. {html.escape(name)}\n"

    text += f"\n👥 <b>Total:</b> {len(rows)}"

    await message.answer(text)


# ============================================================
# ADMIN: CREATE POLL
# ============================================================

async def create_poll_for_channel(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return

    if message.from_user.id != ADMIN_ID:
        return

    channel = get_channel()

    if not channel:
        await message.answer("❌ Pehle 📢 Channel setup karo.")
        return

    chat_id = channel[0]

    rows = get_names(chat_id)

    if len(rows) < 2:
        await message.answer(
            "❌ Poll banane ke liye minimum 2 submitted names chahiye.\n\n"
            f"Current names: {len(rows)}"
        )
        return

    # Keep first occurrence of duplicate text names.
    unique_names = []
    seen = set()

    for _, name in rows:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            unique_names.append(name)

    active = get_active_poll(chat_id)

    if active:
        await message.answer(
            "⚠️ Is channel me ek poll already active hai.\n"
            "Pehle current poll close karo."
        )
        return

    poll_id = create_poll_db(chat_id, unique_names)

    set_drop(chat_id, False)

    try:
        sent = await bot.send_message(
            chat_id,
            "🔥 <b>POLL STARTED</b> 🔥\n\n"
            "Apne favourite name ke box par tap karke vote karo 👇\n\n"
            "🟢 Vote count live update hoga.\n"
            "⚠️ Ek user = ek active vote.",
            reply_markup=poll_keyboard(poll_id),
        )
    except Exception as e:
        logging.exception("Could not send poll: %s", e)
        close_poll_db(poll_id)
        set_drop(chat_id, True)
        await message.answer(
            "❌ Channel me poll send nahi ho paya.\n"
            "Bot permissions check karo."
        )
        return

    set_poll_message_id(poll_id, sent.message_id)

    await message.answer(
        "✅ <b>POLL STARTED</b>\n\n"
        f"📢 {html.escape(channel[2])}\n"
        f"👥 Candidates: {len(unique_names)}",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "🎯 Create Poll")
async def create_poll_button(message: Message):
    if not await admin_only(message):
        return
    await create_poll_for_channel(message)


@dp.message(Command("createpoll"))
async def create_poll_command(message: Message):
    if not await admin_only(message):
        return
    await create_poll_for_channel(message)


# ============================================================
# POLL VOTE
# ============================================================

@dp.callback_query(F.data.startswith("vote:"))
async def vote_callback(callback: CallbackQuery):
    try:
        _, poll_raw, option_raw = callback.data.split(":")
        poll_id = int(poll_raw)
        option_id = int(option_raw)
    except Exception:
        await callback.answer("❌ Invalid vote.", show_alert=True)
        return

    with closing(db()) as conn:
        poll = conn.execute("""
            SELECT chat_id, active FROM polls WHERE poll_id=?
        """, (poll_id,)).fetchone()

        if not poll or not poll[1]:
            await callback.answer("🔴 Poll closed.", show_alert=True)
            return

        option = conn.execute("""
            SELECT name, votes
            FROM poll_options
            WHERE poll_id=? AND option_id=?
        """, (poll_id, option_id)).fetchone()

        if not option:
            await callback.answer("❌ Option not found.", show_alert=True)
            return

        old = conn.execute("""
            SELECT option_id
            FROM poll_votes
            WHERE poll_id=? AND user_id=?
        """, (poll_id, callback.from_user.id)).fetchone()

        if old and old[0] == option_id:
            # Same button tapped again: no duplicate vote.
            await callback.answer(
                f"ℹ️ Aap already {option[0]} ko vote kar chuke ho.",
                show_alert=False,
            )
            return

        # Move an existing vote if user chose another candidate.
        if old:
            old_option_id = old[0]
            conn.execute("""
                UPDATE poll_options
                SET votes=CASE WHEN votes>0 THEN votes-1 ELSE 0 END
                WHERE poll_id=? AND option_id=?
            """, (poll_id, old_option_id))

            conn.execute("""
                UPDATE poll_votes
                SET option_id=?
                WHERE poll_id=? AND user_id=?
            """, (option_id, poll_id, callback.from_user.id))
        else:
            conn.execute("""
                INSERT INTO poll_votes(poll_id,user_id,option_id)
                VALUES(?,?,?)
            """, (poll_id, callback.from_user.id, option_id))

        conn.execute("""
            UPDATE poll_options
            SET votes=votes+1
            WHERE poll_id=? AND option_id=?
        """, (poll_id, option_id))

        conn.commit()

    try:
        await callback.message.edit_reply_markup(
            reply_markup=poll_keyboard(poll_id)
        )
    except Exception as e:
        logging.warning("Poll keyboard refresh failed: %s", e)

    await callback.answer(f"✅ Vote added: {option[0]}")


# ============================================================
# REFRESH
# ============================================================

@dp.callback_query(F.data.startswith("refresh:"))
async def refresh_callback(callback: CallbackQuery):
    try:
        poll_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("❌ Invalid poll.", show_alert=True)
        return

    poll = get_poll(poll_id)

    if not poll:
        await callback.answer("❌ Poll not found.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(
            reply_markup=poll_keyboard(poll_id)
        )
        await callback.answer("📊 Results refreshed.")
    except Exception:
        await callback.answer("❌ Could not refresh.", show_alert=True)


# ============================================================
# CLOSE POLL
# ============================================================

@dp.callback_query(F.data.startswith("close:"))
async def close_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Admin only.", show_alert=True)
        return

    try:
        poll_id = int(callback.data.split(":")[1])
    except Exception:
        await callback.answer("❌ Invalid poll.", show_alert=True)
        return

    poll = get_poll(poll_id)

    if not poll:
        await callback.answer("❌ Poll not found.", show_alert=True)
        return

    close_poll_db(poll_id)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        "🛑 <b>POLL CLOSED</b>\n\n"
        "Voting is no longer available."
    )

    await callback.answer("Poll closed.")


# ============================================================
# ADMIN: STATUS
# ============================================================

@dp.message(F.text == "📊 Poll Status")
async def status_handler(message: Message):
    if message.chat.type != ChatType.PRIVATE or not await admin_only(message):
        return

    channel = get_channel()

    if not channel:
        await message.answer(
            "📊 <b>STATUS</b>\n\n"
            "📢 Channel: Not configured"
        )
        return

    chat_id = channel[0]
    names = get_names(chat_id)
    drop = "🟢 OPEN" if is_drop_active(chat_id) else "🔴 CLOSED"
    active_poll = get_active_poll(chat_id)

    poll_state = "🟢 ACTIVE" if active_poll else "🔴 NONE"

    await message.answer(
        "📊 <b>BOT STATUS</b>\n\n"
        f"📢 Channel: {html.escape(channel[2])}\n"
        f"🔥 Name Drop: {drop}\n"
        f"👥 Names: {len(names)}\n"
        f"🎯 Poll: {poll_state}"
    )


# ============================================================
# ADMIN: CLEAR NAMES
# ============================================================

@dp.message(F.text == "🗑 Clear Names")
async def clear_handler(message: Message):
    if message.chat.type != ChatType.PRIVATE or not await admin_only(message):
        return

    channel = get_channel()

    if not channel:
        await message.answer("❌ Pehle channel connect karo.")
        return

    active_poll = get_active_poll(channel[0])

    if active_poll:
        await message.answer(
            "⚠️ Active poll hai. Pehle poll close karo."
        )
        return

    clear_names(channel[0])

    await message.answer(
        "🗑 <b>All submitted names cleared.</b>",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# CLOSE MENU
# ============================================================

@dp.message(F.text == "❌ Close Menu")
async def close_menu(message: Message, state: FSMContext):
    await state.clear()

    if message.chat.type != ChatType.PRIVATE:
        return

    await message.answer(
        "Keyboard menu close kar diya.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Open Menu")]],
            resize_keyboard=True,
            is_persistent=True,
        ),
    )


@dp.message(F.text == "📱 Open Menu")
async def open_menu(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return

    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👑 Admin Menu",
            reply_markup=admin_keyboard(),
        )
    else:
        await message.answer(
            "🎀 User Menu",
            reply_markup=user_keyboard(),
        )


# ============================================================
# UNKNOWN COMMAND
# ============================================================

@dp.message(F.text.startswith("/"))
async def unknown_command(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        if message.from_user.id == ADMIN_ID:
            await message.answer(
                "❌ Unknown command.\n\n"
                "Admin: /God"
            )
        else:
            await message.answer(
                "❌ Unknown command.\n\n"
                "User menu ke liye /start dabao."
            )


# ============================================================
# MAIN
# ============================================================

async def main():
    init_db()

    print("==========================================")
    print("🚀 GOD POLL PRO BOT")
    print("==========================================")

    me = await bot.get_me()

    print(f"✅ Connected: @{me.username}")
    print(f"🆔 Bot ID: {me.id}")
    print(f"👑 Admin ID: {ADMIN_ID}")

    await bot.delete_webhook(drop_pending_updates=True)

    print("✅ Webhook cleared")
    print("🚀 Polling started")
    print("==========================================")

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


if __name__ == "__main__":
    asyncio.run(main())
