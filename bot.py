import html
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ----------------------------
# Configuration
# ----------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
RESELLER_ID = int(os.getenv("RESELLER_ID", "0"))
DB_PATH = Path(os.getenv("DB_PATH", "orders.db"))

NUMERIC_PRODUCTS = {
    "55", "86", "165", "172", "257", "275", "343", "429", "514", "565",
    "600", "706", "878", "963", "1049", "1135", "1220", "1412", "1584",
    "1669", "2195", "2538", "2901", "3158", "3688", "4394", "5100",
    "5532", "6055", "6752", "7030", "7727", "9288",
}

KEYWORD_PRODUCTS = {"wp", "wp 2", "wp 3", "wp 4", "wp 5", "tp", "web", "meb"}

STATUS_WAITING = "waiting"
STATUS_RESTOCKING = "restocking"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ----------------------------
# Database
# ----------------------------

@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                product TEXT NOT NULL,
                original_command TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                eta TEXT,
                reseller_chat_id INTEGER NOT NULL,
                reseller_message_id INTEGER,
                admin_chat_id INTEGER NOT NULL,
                admin_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)"
        )


def create_order(
    user_id: str,
    zone_id: str,
    product: str,
    original_command: str,
    reseller_chat_id: int,
    admin_chat_id: int,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO orders (
                user_id, zone_id, product, original_command, status, eta,
                reseller_chat_id, admin_chat_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                user_id,
                zone_id,
                product,
                original_command,
                STATUS_WAITING,
                reseller_chat_id,
                admin_chat_id,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def set_message_ids(
    order_id: int,
    reseller_message_id: Optional[int] = None,
    admin_message_id: Optional[int] = None,
) -> None:
    fields = []
    values = []

    if reseller_message_id is not None:
        fields.append("reseller_message_id = ?")
        values.append(reseller_message_id)
    if admin_message_id is not None:
        fields.append("admin_message_id = ?")
        values.append(admin_message_id)

    if not fields:
        return

    values.extend([datetime.now(timezone.utc).isoformat(), order_id])
    with db() as conn:
        conn.execute(
            f"UPDATE orders SET {', '.join(fields)}, updated_at = ? WHERE id = ?",
            values,
        )


def get_order(order_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()


def update_order_status(order_id: int, new_status: str, eta: Optional[str] = None):
    """
    Atomically updates an unfinished order.
    Returns (updated, row_after_update).
    """
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()

        if row is None:
            return False, None

        if row["status"] in (STATUS_SUCCESS, STATUS_FAILED):
            return False, row

        conn.execute(
            """
            UPDATE orders
            SET status = ?, eta = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                eta,
                datetime.now(timezone.utc).isoformat(),
                order_id,
            ),
        )
        updated_row = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return True, updated_row


def recent_orders(limit: int = 10):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def pending_orders(limit: int = 30):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM orders
            WHERE status IN (?, ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (STATUS_WAITING, STATUS_RESTOCKING, limit),
        ).fetchall()


def today_stats():
    today_utc = datetime.now(timezone.utc).date().isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM orders
            WHERE substr(created_at, 1, 10) = ?
            GROUP BY status
            """,
            (today_utc,),
        ).fetchall()

    counts = {
        STATUS_WAITING: 0,
        STATUS_RESTOCKING: 0,
        STATUS_SUCCESS: 0,
        STATUS_FAILED: 0,
    }
    for row in rows:
        counts[row["status"]] = row["count"]
    return counts


# ----------------------------
# Formatting
# ----------------------------

def public_order_id(order_id: int) -> str:
    return f"{order_id:07d}"


def product_display(product: str) -> str:
    if product.startswith("wp"):
        count = product.split(maxsplit=1)
        return "WP" if len(count) == 1 else f"WP ×{count[1]}"
    return product.upper() if product in {"tp", "web", "meb"} else f"{product} 💎"


def reseller_text(order) -> str:
    order_no = public_order_id(order["id"])
    status = order["status"]

    if status == STATUS_WAITING:
        first_line = f"⏳ #{order_no} Waiting"
    elif status == STATUS_RESTOCKING:
        first_line = f"🟡 #{order_no} • {order['eta']}"
    elif status == STATUS_SUCCESS:
        first_line = f"✅ #{order_no} Recharged"
    else:
        first_line = f"❌ #{order_no} Failed"

    return (
        f"{first_line}\n"
        f"{html.escape(order['user_id'])} • {html.escape(order['zone_id'])}\n"
        f"{html.escape(product_display(order['product']))}"
    )


def admin_text(order) -> str:
    order_no = public_order_id(order["id"])
    status = order["status"]

    if status == STATUS_WAITING:
        label = "⏳ Pending"
    elif status == STATUS_RESTOCKING:
        label = f"🟡 {order['eta']}"
    elif status == STATUS_SUCCESS:
        label = "✅ Success"
    else:
        label = "❌ Failed"

    command = html.escape(order["original_command"])
    return f"#{order_no} • {label}\n<code>{command}</code>"


def status_icon(status: str) -> str:
    return {
        STATUS_WAITING: "⏳",
        STATUS_RESTOCKING: "🟡",
        STATUS_SUCCESS: "✅",
        STATUS_FAILED: "❌",
    }.get(status, "•")


def order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Success", callback_data=f"success:{order_id}"
                ),
                InlineKeyboardButton(
                    "❌ Failed", callback_data=f"failed:{order_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "⏳ Restocking 5–10 min",
                    callback_data=f"eta_5_10:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏳ Restocking 10–20 min",
                    callback_data=f"eta_10_20:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⏳ Restocking 20–30 min",
                    callback_data=f"eta_20_30:{order_id}",
                )
            ],
        ]
    )


# ----------------------------
# Parsing and authorization
# ----------------------------

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_reseller(user_id: int) -> bool:
    return user_id == RESELLER_ID


def normalize_product(parts: list[str]) -> Optional[str]:
    if len(parts) == 1:
        candidate = parts[0].lower()
        if candidate in NUMERIC_PRODUCTS or candidate in KEYWORD_PRODUCTS:
            return candidate
        return None

    if len(parts) == 2 and parts[0].lower() == "wp" and parts[1] in {"2", "3", "4", "5"}:
        return f"wp {parts[1]}"

    return None


def parse_mk(text: str):
    if "\n" in text.strip():
        return None, "❌ Send one /mk order per message."

    parts = text.strip().split()
    if not parts or parts[0].split("@")[0].lower() != "/mk":
        return None, "❌ Invalid command."

    # Expected:
    # /mk USER ZONE PRODUCT
    # /mk USER ZONE wp COUNT
    if len(parts) not in (4, 5):
        return None, "❌ Use: /mk USER_ID ZONE_ID PRODUCT"

    user_id = parts[1]
    zone_id = parts[2]
    product_parts = parts[3:]

    if not re.fullmatch(r"\d{3,20}", user_id):
        return None, "❌ Invalid User ID."

    if not re.fullmatch(r"\d{1,10}", zone_id):
        return None, "❌ Invalid Zone ID."

    product = normalize_product(product_parts)
    if product is None:
        return None, "❌ Invalid diamond package or product."

    canonical_product = product
    original_command = f"/mk {user_id} {zone_id} {canonical_product} r"

    return {
        "user_id": user_id,
        "zone_id": zone_id,
        "product": canonical_product,
        "original_command": original_command,
    }, None


# ----------------------------
# Telegram handlers
# ----------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.effective_message is None:
        return

    if is_admin(user.id):
        await update.effective_message.reply_text(
            "👑 Admin ready\n/pending • /find • /history • /stats"
        )
    elif is_reseller(user.id):
        await update.effective_message.reply_text(
            "✅ Recharge bot ready\nSend one /mk order per message."
        )
    else:
        await update.effective_message.reply_text("⛔ Access denied.")


async def mk_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    if not is_reseller(user.id):
        await message.reply_text("⛔ Only the authorized reseller can submit orders.")
        return

    parsed, error = parse_mk(message.text or "")
    if error:
        await message.reply_text(error)
        return

    order_id = create_order(
        user_id=parsed["user_id"],
        zone_id=parsed["zone_id"],
        product=parsed["product"],
        original_command=parsed["original_command"],
        reseller_chat_id=message.chat_id,
        admin_chat_id=ADMIN_ID,
    )
    order = get_order(order_id)

    reseller_msg = await message.reply_text(
        reseller_text(order),
        parse_mode=ParseMode.HTML,
    )
    set_message_ids(order_id, reseller_message_id=reseller_msg.message_id)

    try:
        admin_msg = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text(order),
            parse_mode=ParseMode.HTML,
            reply_markup=order_keyboard(order_id),
        )
        set_message_ids(order_id, admin_message_id=admin_msg.message_id)
    except Forbidden:
        logger.exception("Cannot message admin. Admin must start the bot first.")
        await reseller_msg.edit_text(
            f"⚠️ #{public_order_id(order_id)} Not submitted\n"
            f"{html.escape(parsed['user_id'])} • {html.escape(parsed['zone_id'])}\n"
            f"Contact admin",
            parse_mode=ParseMode.HTML,
        )


async def order_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if query is None or user is None:
        return

    if not is_admin(user.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    await query.answer()

    try:
        action, raw_order_id = query.data.split(":", 1)
        order_id = int(raw_order_id)
    except (AttributeError, ValueError):
        await query.answer("Invalid action.", show_alert=True)
        return

    action_map = {
        "success": (STATUS_SUCCESS, None),
        "failed": (STATUS_FAILED, None),
        "eta_5_10": (STATUS_RESTOCKING, "5–10 min"),
        "eta_10_20": (STATUS_RESTOCKING, "10–20 min"),
        "eta_20_30": (STATUS_RESTOCKING, "20–30 min"),
    }

    if action not in action_map:
        await query.answer("Unknown action.", show_alert=True)
        return

    new_status, eta = action_map[action]
    updated, order = update_order_status(order_id, new_status, eta)

    if order is None:
        await query.answer("Order not found.", show_alert=True)
        return

    if not updated:
        await query.answer("This order is already finished.", show_alert=True)
        return

    admin_markup = (
        order_keyboard(order_id)
        if new_status == STATUS_RESTOCKING
        else None
    )

    try:
        await query.edit_message_text(
            text=admin_text(order),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_markup,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.exception("Failed to edit admin message")

    if order["reseller_message_id"]:
        try:
            await context.bot.edit_message_text(
                chat_id=order["reseller_chat_id"],
                message_id=order["reseller_message_id"],
                text=reseller_text(order),
                parse_mode=ParseMode.HTML,
            )
        except (BadRequest, Forbidden):
            logger.exception("Failed to edit reseller message for order %s", order_id)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    if not (is_admin(user.id) or is_reseller(user.id)):
        await message.reply_text("⛔ Access denied.")
        return

    limit = 10
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 30))
        except ValueError:
            await message.reply_text("❌ Use: /history or /history 20")
            return

    rows = recent_orders(limit)
    if not rows:
        await message.reply_text("No orders yet.")
        return

    lines = [
        f"{public_order_id(row['id'])} {status_icon(row['status'])} {product_display(row['product'])}"
        for row in rows
    ]
    await message.reply_text("\n".join(lines))


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    if not is_admin(user.id):
        await message.reply_text("⛔ Admin only.")
        return

    rows = pending_orders()
    if not rows:
        await message.reply_text("✅ No pending orders.")
        return

    lines = []
    for row in rows:
        eta = f" {row['eta']}" if row["eta"] else ""
        lines.append(
            f"{public_order_id(row['id'])} {status_icon(row['status'])}{eta} "
            f"• {product_display(row['product'])}"
        )
    await message.reply_text("\n".join(lines))


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    if not is_admin(user.id):
        await message.reply_text("⛔ Admin only.")
        return

    if len(context.args) != 1 or not context.args[0].lstrip("#").isdigit():
        await message.reply_text("❌ Use: /find 0000123")
        return

    order_id = int(context.args[0].lstrip("#"))
    order = get_order(order_id)
    if order is None:
        await message.reply_text("❌ Order not found.")
        return

    markup = (
        order_keyboard(order_id)
        if order["status"] in (STATUS_WAITING, STATUS_RESTOCKING)
        else None
    )
    await message.reply_text(
        admin_text(order),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return

    if not is_admin(user.id):
        await message.reply_text("⛔ Admin only.")
        return

    counts = today_stats()
    pending_total = counts[STATUS_WAITING] + counts[STATUS_RESTOCKING]
    await message.reply_text(
        "📊 Today (UTC)\n"
        f"✅ {counts[STATUS_SUCCESS]} • ❌ {counts[STATUS_FAILED]}\n"
        f"⏳ {pending_total}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram bot error", exc_info=context.error)


def validate_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if ADMIN_ID <= 0:
        missing.append("ADMIN_ID")
    if RESELLER_ID <= 0:
        missing.append("RESELLER_ID")
    if ADMIN_ID == RESELLER_ID and ADMIN_ID > 0:
        raise RuntimeError("ADMIN_ID and RESELLER_ID must be different.")

    if missing:
        raise RuntimeError(
            "Missing or invalid environment variables: " + ", ".join(missing)
        )


def main() -> None:
    validate_config()
    init_db()

    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("mk", mk_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(
        CallbackQueryHandler(
            order_button,
            pattern=r"^(success|failed|eta_5_10|eta_10_20|eta_20_30):\d+$",
        )
    )
    application.add_error_handler(error_handler)

    logger.info("Bot started.")
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
