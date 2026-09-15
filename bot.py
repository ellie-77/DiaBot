"""
Anonymous-inbox Telegram bot.

Features
- /start and /links: buttons that link to your channels/groups
- Anonymous chat: any user can send a text message that is stored anonymously
- Admin inbox (/inbox): each new message has "Answer" and "Show user details" buttons
- Answer flow: admin writes an answer -> bot shows a preview (user message quoted,
  answer below) with "Push to channel" and "Edit text" buttons

Everything user-facing (channel links, button labels, messages) lives in config.json.
If config.json does not exist, a default one is written on first run.
"""

import html
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("anonbot")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

# --------------------------------------------------------------------------- #
# Default config (written to config.json if the file is missing)
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "bot_token": "PUT_YOUR_BOT_TOKEN_HERE",
    "admin_ids": [123456789],
    "target_channel": "@your_channel",
    "database_file": "bot.db",
    "links": [
        {"label": "📢 My Channel", "url": "https://t.me/your_channel"},
        {"label": "💬 My Group", "url": "https://t.me/your_group"},
        {"label": "🌐 Website", "url": "https://example.com"},
    ],
    "buttons": {
        "send_anonymous": "✉️ Send anonymous message",
        "answer": "✍️ Answer",
        "show_user": "👤 Show user details",
        "push_to_channel": "📤 Push to channel",
        "edit_text": "✏️ Edit text",
        "cancel": "❌ Cancel",
    },
    "messages": {
        "welcome": "👋 Welcome!\n\nUse the buttons below to visit my channels, or send me an anonymous message.",
        "links_intro": "🔗 Here are my channels and groups:",
        "anon_prompt": "✍️ Write your message now. It will be delivered anonymously.",
        "anon_received": "✅ Your message has been received. Thank you!",
        "text_only": "⚠️ Only text messages are supported right now.",
        "not_admin": "⛔ This command is for admins only.",
        "admin_help": "🛠 Admin commands:\n/inbox — show new anonymous messages\n/cancel — cancel the current action",
        "inbox_empty": "📭 Inbox is empty — no new messages.",
        "inbox_header": "📥 You have {count} new message(s):",
        "inbox_item": "📨 Message #{id}\n🕒 {date}\n\n{text}",
        "new_message_alert": "🔔 New anonymous message!\n\n📨 Message #{id}\n🕒 {date}\n\n{text}",
        "user_details": "👤 User details for message #{id}\n\nID: <code>{user_id}</code>\nUsername: {username}\nName: {full_name}",
        "ask_answer": "✍️ Write your answer to message #{id}:",
        "preview": "<blockquote>{user_text}</blockquote>\n\n{answer}",
        "channel_post": "<blockquote>{user_text}</blockquote>\n\n{answer}",
        "pushed": "✅ Pushed to channel.",
        "push_failed": "❌ Could not post to channel: {error}",
        "cancelled": "🚫 Cancelled.",
        "nothing_to_cancel": "Nothing to cancel.",
        "message_not_found": "⚠️ Message not found.",
    },
    "commands": {
        "start": "Start the bot",
        "links": "Show my channels and groups",
        "anon": "Send an anonymous message",
        "inbox": "(admin) Show new messages",
        "cancel": "(admin) Cancel current action",
    },
}


def load_config() -> dict:
    """Load config.json, creating it from defaults if missing.
    Missing keys are filled in from DEFAULT_CONFIG so partial configs still work."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.warning("config.json not found – default written to %s. Edit it and restart.", CONFIG_PATH)
    user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    for key, value in user_cfg.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


CFG = load_config()
MSG = CFG["messages"]
BTN = CFG["buttons"]
ADMIN_IDS = {int(x) for x in CFG["admin_ids"]}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                username   TEXT,
                full_name  TEXT,
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'new',
                answer     TEXT
            )
            """
        )
        self.conn.commit()

    def add(self, user_id: int, username: str | None, full_name: str, text: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages (user_id, username, full_name, text, created_at) VALUES (?,?,?,?,?)",
            (user_id, username, full_name, text, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, msg_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()

    def new_messages(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE status='new' ORDER BY id"
        ).fetchall()

    def set_answer(self, msg_id: int, answer: str) -> None:
        self.conn.execute("UPDATE messages SET answer=? WHERE id=?", (answer, msg_id))
        self.conn.commit()

    def mark_answered(self, msg_id: int) -> None:
        self.conn.execute("UPDATE messages SET status='answered' WHERE id=?", (msg_id,))
        self.conn.commit()


DBASE = DB(str(BASE_DIR / CFG["database_file"]))


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
def links_keyboard(include_anon: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(link["label"], url=link["url"])] for link in CFG["links"]]
    if include_anon:
        rows.append([InlineKeyboardButton(BTN["send_anonymous"], callback_data="anon")])
    return InlineKeyboardMarkup(rows)


def inbox_item_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(BTN["answer"], callback_data=f"ans:{msg_id}"),
                InlineKeyboardButton(BTN["show_user"], callback_data=f"usr:{msg_id}"),
            ]
        ]
    )


def preview_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(BTN["push_to_channel"], callback_data=f"push:{msg_id}"),
                InlineKeyboardButton(BTN["edit_text"], callback_data=f"edit:{msg_id}"),
            ],
            [InlineKeyboardButton(BTN["cancel"], callback_data="cancel")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN["cancel"], callback_data="cancel")]])


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def fmt_item(row: sqlite3.Row, template: str) -> str:
    return template.format(id=row["id"], date=row["created_at"], text=html.escape(row["text"]))


def fmt_preview(row: sqlite3.Row, answer: str, template: str) -> str:
    return template.format(
        user_text=html.escape(row["text"]), answer=html.escape(answer), id=row["id"]
    )


# --------------------------------------------------------------------------- #
# User commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(MSG["welcome"], reply_markup=links_keyboard())
    if is_admin(update.effective_user.id):
        await update.message.reply_text(MSG["admin_help"])


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(MSG["links_intro"], reply_markup=links_keyboard())


async def cmd_anon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["anon_mode"] = True
    await update.message.reply_text(MSG["anon_prompt"])


async def cb_anon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data["anon_mode"] = True
    await query.message.reply_text(MSG["anon_prompt"])


# --------------------------------------------------------------------------- #
# Admin commands
# --------------------------------------------------------------------------- #
async def cmd_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(MSG["not_admin"])
        return
    rows = DBASE.new_messages()
    if not rows:
        await update.message.reply_text(MSG["inbox_empty"])
        return
    await update.message.reply_text(MSG["inbox_header"].format(count=len(rows)))
    for row in rows:
        await update.message.reply_text(
            fmt_item(row, MSG["inbox_item"]),
            reply_markup=inbox_item_keyboard(row["id"]),
            parse_mode=ParseMode.HTML,
        )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(MSG["not_admin"])
        return
    if context.user_data.pop("answer_for", None) is not None:
        await update.message.reply_text(MSG["cancelled"])
    else:
        await update.message.reply_text(MSG["nothing_to_cancel"])


# --------------------------------------------------------------------------- #
# Callback buttons (admin)
# --------------------------------------------------------------------------- #
async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer(MSG["not_admin"], show_alert=True)
        return
    await query.answer()

    data = query.data
    if data == "cancel":
        context.user_data.pop("answer_for", None)
        await query.edit_message_text(MSG["cancelled"])
        return

    action, _, raw_id = data.partition(":")
    msg_id = int(raw_id)
    row = DBASE.get(msg_id)
    if row is None:
        await query.message.reply_text(MSG["message_not_found"])
        return

    if action == "usr":
        username = f"@{row['username']}" if row["username"] else "—"
        await query.message.reply_text(
            MSG["user_details"].format(
                id=row["id"],
                user_id=row["user_id"],
                username=html.escape(username),
                full_name=html.escape(row["full_name"] or "—"),
            ),
            parse_mode=ParseMode.HTML,
        )

    elif action in ("ans", "edit"):
        # Next text message from this admin is the answer
        context.user_data["answer_for"] = msg_id
        await query.message.reply_text(
            MSG["ask_answer"].format(id=msg_id), reply_markup=cancel_keyboard()
        )

    elif action == "push":
        answer = row["answer"]
        if not answer:
            await query.message.reply_text(MSG["message_not_found"])
            return
        try:
            await context.bot.send_message(
                chat_id=CFG["target_channel"],
                text=fmt_preview(row, answer, MSG["channel_post"]),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to push to channel")
            await query.message.reply_text(MSG["push_failed"].format(error=html.escape(str(exc))))
            return
        DBASE.mark_answered(msg_id)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(MSG["pushed"])


# --------------------------------------------------------------------------- #
# Plain text messages
# --------------------------------------------------------------------------- #
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = update.message.text

    # ---- admin writing an answer ------------------------------------------
    if is_admin(user.id) and context.user_data.get("answer_for") is not None:
        msg_id = context.user_data.pop("answer_for")
        row = DBASE.get(msg_id)
        if row is None:
            await update.message.reply_text(MSG["message_not_found"])
            return
        DBASE.set_answer(msg_id, text)
        await update.message.reply_text(
            fmt_preview(row, text, MSG["preview"]),
            reply_markup=preview_keyboard(msg_id),
            parse_mode=ParseMode.HTML,
        )
        return

    # ---- admin idle: show help -------------------------------------------
    if is_admin(user.id):
        await update.message.reply_text(MSG["admin_help"])
        return

    # ---- normal user: store as anonymous message --------------------------
    context.user_data.pop("anon_mode", None)
    msg_id = DBASE.add(user.id, user.username, user.full_name, text)
    await update.message.reply_text(MSG["anon_received"])

    row = DBASE.get(msg_id)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=fmt_item(row, MSG["new_message_alert"]),
                reply_markup=inbox_item_keyboard(msg_id),
                parse_mode=ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001  (admin hasn't started the bot, etc.)
            log.warning("Could not notify admin %s", admin_id)


async def on_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(MSG["text_only"])


# --------------------------------------------------------------------------- #
# App setup
# --------------------------------------------------------------------------- #
async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [BotCommand(name, desc) for name, desc in CFG["commands"].items()]
    )


def build_app() -> Application:
    app = Application.builder().token(CFG["bot_token"]).post_init(post_init).build()

    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, private))
    app.add_handler(CommandHandler("links", cmd_links, private))
    app.add_handler(CommandHandler("anon", cmd_anon, private))
    app.add_handler(CommandHandler("inbox", cmd_inbox, private))
    app.add_handler(CommandHandler("cancel", cmd_cancel, private))

    app.add_handler(CallbackQueryHandler(cb_anon, pattern=r"^anon$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^(ans|usr|push|edit):\d+$|^cancel$"))

    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & ~filters.TEXT & ~filters.COMMAND, on_non_text))
    return app


def main() -> None:
    if CFG["bot_token"] == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Edit config.json and set your bot_token first.")
    if ADMIN_IDS == {123456789}:
        log.warning("admin_ids still has the placeholder value – set your own Telegram user id.")
    app = build_app()
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
