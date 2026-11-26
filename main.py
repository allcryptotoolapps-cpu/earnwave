
import asyncio
import os
import math
from datetime import datetime, timezone, timedelta

import asyncpg
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton


TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "10000"))

COOLDOWN_SECONDS = 30
REWARD_POINTS = 10
MIN_WITHDRAWAL = 100
REFERRAL_BONUS = 10

ADMIN_IDS = {8435970103}

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://", "postgresql://", 1
    )

bot = Bot(token=TOKEN)
dp = Dispatcher()
db_pool = None

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎬 Watch Ads")],
        [KeyboardButton(text="📋 Tasks"), KeyboardButton(text="💰 Balance")],
        [KeyboardButton(text="💳 Withdraw")]
    ],
    resize_keyboard=True
)


# ---------------- DATABASE ----------------

async def setup_database():
    global db_pool

    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                points BIGINT NOT NULL DEFAULT 0,
                last_ad_at TIMESTAMPTZ,
                is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                referred_by BIGINT,
                referral_paid BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)

        # Existing database migration
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS referred_by BIGINT
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS referral_paid BOOLEAN NOT NULL DEFAULT FALSE
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                reward BIGINT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                submission_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                task_id BIGINT NOT NULL,
                proof TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                withdrawal_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount BIGINT NOT NULL,
                wallet TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

    print("PostgreSQL connected successfully!")


async def get_setting(key, default):
    async with db_pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT value FROM settings WHERE key = $1", key
        )
    return int(value) if value is not None else default


async def set_setting(key, value):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value
        """, key, str(value))


async def register_user(user_id, referred_by=None):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval("""
                INSERT INTO users (user_id, points, referred_by)
                VALUES ($1, 0, $2)
                ON CONFLICT (user_id) DO NOTHING
                RETURNING user_id
            """, user_id, referred_by)

            if inserted and referred_by and referred_by != user_id:
                referrer = await conn.fetchval(
                    "SELECT user_id FROM users WHERE user_id = $1",
                    referred_by
                )

                if referrer:
                    bonus = await get_setting("referral_bonus", REFERRAL_BONUS)

                    await conn.execute("""
                        UPDATE users SET points = points + $1
                        WHERE user_id = $2
                    """, bonus, referred_by)

                    await conn.execute("""
                        UPDATE users SET referral_paid = TRUE
                        WHERE user_id = $1
                    """, user_id)


async def get_user(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT points, last_ad_at, is_banned
            FROM users WHERE user_id = $1
        """, user_id)


async def is_admin(user_id):
    return user_id in ADMIN_IDS


async def allowed(message):
    if message.from_user.id in ADMIN_IDS:
        return True

    user = await get_user(message.from_user.id)

    if user and user["is_banned"]:
        await message.answer("⛔ Your account is banned.")
        return False

    return True


# ---------------- START ----------------

@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id

    args = (message.text or "").split()
    referred_by = None

    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referred_by = int(args[1][4:])
        except ValueError:
            referred_by = None

    await register_user(user_id, referred_by)

    if not await allowed(message):
        return

    await message.answer(
        "Welcome to EarnWave! 🌟\n\n"
        "Earn points by watching ads and completing tasks.",
        reply_markup=keyboard
    )


# ---------------- ADMIN PANEL ----------------

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ Not authorized.")
        return

    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval(
            "SELECT COUNT(*) FROM users"
        )
        total_points = await conn.fetchval(
            "SELECT COALESCE(SUM(points), 0) FROM users"
        )

    reward = await get_setting("ad_reward", REWARD_POINTS)
    minimum = await get_setting("min_withdraw", MIN_WITHDRAWAL)

    await message.answer(
        "🛠 EARNWAVE ADMIN PANEL\n\n"
        f"👥 Users: {total_users}\n"
        f"💰 Total Points: {total_points}\n"
        f"🎬 Ad Reward: {reward}\n"
        f"💳 Minimum Withdrawal: {minimum}\n\n"
        "Use /helpadmin for commands."
    )


@dp.message(Command("helpadmin"))
async def admin_help(message: Message):
    if not await is_admin(message.from_user.id):
        return

    await message.answer(
        "🛠 ADMIN COMMANDS\n\n"
        "/users - User list\n"
        "/addpoints ID AMOUNT\n"
        "/removepoints ID AMOUNT\n"
        "/ban ID\n"
        "/unban ID\n\n"
        "/addtask REWARD | TASK NAME\n"
        "/tasklist - All tasks\n"
        "/deltask TASK_ID\n"
        "/approve SUBMISSION_ID\n"
        "/reject SUBMISSION_ID\n"
        "/submissions - Pending submissions\n\n"
        "/withdrawals - Pending withdrawals\n"
        "/approvewithdraw ID\n"
        "/rejectwithdraw ID\n\n"
        "/broadcast MESSAGE\n"
        "/setadreward AMOUNT\n"
        "/setminwithdraw AMOUNT\n"
        "/setrefbonus AMOUNT\n"
        "/referral - Your referral link"
    )


# ---------------- EXISTING USER MANAGEMENT ----------------

@dp.message(Command("users"))
async def admin_users(message: Message):
    if not await is_admin(message.from_user.id):
        return

    async with db_pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT user_id, points, is_banned
            FROM users ORDER BY points DESC LIMIT 20
        """)

    if not users:
        await message.answer("No users found.")
        return

    result = "👥 USER LIST\n\n"

    for user in users:
        status = "BANNED" if user["is_banned"] else "ACTIVE"
        result += (
            f"ID: {user['user_id']} | "
            f"Points: {user['points']} | {status}\n"
        )

    await message.answer(result)


@dp.message(Command("addpoints"))
async def add_points(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        _, user_id, amount = message.text.split()
        user_id, amount = int(user_id), int(amount)

        if amount <= 0:
            raise ValueError

        async with db_pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE users SET points = points + $1
                WHERE user_id = $2
            """, amount, user_id)

        if result == "UPDATE 0":
            await message.answer("User not found.")
        else:
            await message.answer(
                f"✅ Added {amount} points to {user_id}."
            )
    except (ValueError, TypeError):
        await message.answer("Use: /addpoints USER_ID AMOUNT")


@dp.message(Command("removepoints"))
async def remove_points(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        _, user_id, amount = message.text.split()
        user_id, amount = int(user_id), int(amount)

        if amount <= 0:
            raise ValueError

        async with db_pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE users
                SET points = GREATEST(points - $1, 0)
                WHERE user_id = $2
            """, amount, user_id)

        if result == "UPDATE 0":
            await message.answer("User not found.")
        else:
            await message.answer(
                f"✅ Removed {amount} points from {user_id}."
            )
    except (ValueError, TypeError):
        await message.answer("Use: /removepoints USER_ID AMOUNT")


# ---------------- BAN / UNBAN ----------------

@dp.message(Command("ban"))
async def ban_user(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        user_id = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE users SET is_banned = TRUE
                WHERE user_id = $1
            """, user_id)

        if result == "UPDATE 0":
            await message.answer("User not found.")
        else:
            await message.answer(f"🚫 User {user_id} banned.")

            try:
                await bot.send_message(user_id, "⛔ Your account has been banned.")
            except Exception:
                pass

    except (ValueError, IndexError):
        await message.answer("Use: /ban USER_ID")


@dp.message(Command("unban"))
async def unban_user(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        user_id = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE users SET is_banned = FALSE
                WHERE user_id = $1
            """, user_id)

        if result == "UPDATE 0":
            await message.answer("User not found.")
        else:
            await message.answer(f"✅ User {user_id} unbanned.")

    except (ValueError, IndexError):
        await message.answer("Use: /unban USER_ID")


# ---------------- TASK MANAGEMENT ----------------

@dp.message(Command("addtask"))
async def add_task(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        data = message.text.split(maxsplit=1)[1]
        reward_text, title = data.split("|", 1)

        reward = int(reward_text.strip())
        title = title.strip()

        if reward <= 0 or not title:
            raise ValueError

        async with db_pool.acquire() as conn:
            task_id = await conn.fetchval("""
                INSERT INTO tasks (title, reward)
                VALUES ($1, $2)
                RETURNING task_id
            """, title, reward)

        await message.answer(
            f"✅ Task added!\nID: {task_id}\n"
            f"Reward: {reward}\nTitle: {title}"
        )

    except (ValueError, IndexError):
        await message.answer(
            "Use:\n/addtask 50 | Join our Telegram channel"
        )


@dp.message(Command("tasklist"))
async def task_list(message: Message):
    if not await is_admin(message.from_user.id):
        return

    async with db_pool.acquire() as conn:
        tasks = await conn.fetch("""
            SELECT task_id, title, reward, active
            FROM tasks ORDER BY task_id DESC
        """)

    if not tasks:
        await message.answer("No tasks available.")
        return

    text = "📋 TASK LIST\n\n"

    for task in tasks:
        status = "Active" if task["active"] else "Deleted"
        text += (
            f"ID: {task['task_id']}\n"
            f"{task['title']}\n"
            f"Reward: {task['reward']} | {status}\n\n"
        )

    await message.answer(text)


@dp.message(Command("deltask"))
async def delete_task(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        task_id = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE tasks SET active = FALSE
                WHERE task_id = $1
            """, task_id)

        if result == "UPDATE 0":
            await message.answer("Task not found.")
        else:
            await message.answer(f"🗑 Task {task_id} deleted.")

    except (ValueError, IndexError):
        await message.answer("Use: /deltask TASK_ID")


@dp.message(Command("submit"))
async def submit_task(message: Message):
    if not await allowed(message):
        return

    try:
        _, task_id, proof = message.text.split(maxsplit=2)
        task_id = int(task_id)

        async with db_pool.acquire() as conn:
            task = await conn.fetchrow("""
                SELECT task_id FROM tasks
                WHERE task_id = $1 AND active = TRUE
            """, task_id)

            if not task:
                await message.answer("Task not found or inactive.")
                return

            existing = await conn.fetchval("""
                SELECT submission_id FROM submissions
                WHERE user_id = $1 AND task_id = $2
                AND status IN ('pending', 'approved')
            """, message.from_user.id, task_id)

            if existing:
                await message.answer(
                    "You already submitted this task."
                )
                return

            submission_id = await conn.fetchval("""
                INSERT INTO submissions (user_id, task_id, proof)
                VALUES ($1, $2, $3)
                RETURNING submission_id
            """, message.from_user.id, task_id, proof)

        await message.answer(
            f"✅ Submission received!\nSubmission ID: {submission_id}"
        )

    except (ValueError, IndexError):
        await message.answer(
            "Use: /submit TASK_ID YOUR_PROOF"
        )


@dp.message(Command("submissions"))
async def pending_submissions(message: Message):
    if not await is_admin(message.from_user.id):
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT submission_id, user_id, task_id, proof
            FROM submissions
            WHERE status = 'pending'
            ORDER BY submission_id DESC LIMIT 20
        """)

    if not rows:
        await message.answer("No pending submissions.")
        return

    text = "📨 PENDING SUBMISSIONS\n\n"

    for row in rows:
        text += (
            f"Submission: {row['submission_id']}\n"
            f"User: {row['user_id']}\n"
            f"Task: {row['task_id']}\n"
            f"Proof: {row['proof'][:300]}\n\n"
        )

    await message.answer(text)


@dp.message(Command("approve"))
async def approve_submission(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        submission_id = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT s.user_id, t.reward
                    FROM submissions s
                    JOIN tasks t ON t.task_id = s.task_id
                    WHERE s.submission_id = $1
                    AND s.status = 'pending'
                    FOR UPDATE OF s
                """, submission_id)

                if not row:
                    await message.answer("Pending submission not found.")
                    return

                await conn.execute("""
                    UPDATE submissions SET status = 'approved'
                    WHERE submission_id = $1
                """, submission_id)

                await conn.execute("""
                    UPDATE users SET points = points + $1
                    WHERE user_id = $2
                """, row["reward"], row["user_id"])

        await message.answer("✅ Submission approved and reward added.")

        try:
            await bot.send_message(
                row["user_id"],
                f"🎉 Your task was approved! +{row['reward']} points."
            )
        except Exception:
            pass

    except (ValueError, IndexError):
        await message.answer("Use: /approve SUBMISSION_ID")


@dp.message(Command("reject"))
async def reject_submission(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        submission_id = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE submissions SET status = 'rejected'
                WHERE submission_id = $1 AND status = 'pending'
                RETURNING user_id
            """, submission_id)

        if not row:
            await message.answer("Pending submission not found.")
            return

        await message.answer("❌ Submission rejected.")

        try:
            await bot.send_message(
                row["user_id"],
                "Your task submission was rejected."
            )
        except Exception:
            pass

    except (ValueError, IndexError):
        await message.answer("Use: /reject SUBMISSION_ID")


# ---------------- WITHDRAWAL SYSTEM ----------------

@dp.message(Command("withdraw"))
async def create_withdrawal(message: Message):
    if not await allowed(message):
        return

    try:
        _, amount_text, wallet = message.text.split(maxsplit=2)
        amount = int(amount_text)
        user_id = message.from_user.id

        minimum = await get_setting("min_withdraw", MIN_WITHDRAWAL)

        if amount < minimum:
            await message.answer(
                f"Minimum withdrawal is {minimum} points."
            )
            return

        if amount <= 0:
            raise ValueError

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                user = await conn.fetchrow("""
                    SELECT points FROM users
                    WHERE user_id = $1 FOR UPDATE
                """, user_id)

                if not user or user["points"] < amount:
                    await message.answer("Insufficient points.")
                    return

                await conn.execute("""
                    UPDATE users SET points = points - $1
                    WHERE user_id = $2
                """, amount, user_id)

                withdrawal_id = await conn.fetchval("""
                    INSERT INTO withdrawals (user_id, amount, wallet)
                    VALUES ($1, $2, $3)
                    RETURNING withdrawal_id
                """, user_id, amount, wallet)

        await message.answer(
            f"✅ Withdrawal request submitted!\n"
            f"Request ID: {withdrawal_id}\n"
            f"Amount: {amount} points\n"
            "Your points are reserved while the request is pending."
        )

    except (ValueError, IndexError):
        await message.answer(
            "Use: /withdraw AMOUNT YOUR_CRYPTO_WALLET"
        )


@dp.message(Command("withdrawals"))
async def pending_withdrawals(message: Message):
    if not await is_admin(message.from_user.id):
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT withdrawal_id, user_id, amount, wallet
            FROM withdrawals
            WHERE status = 'pending'
            ORDER BY withdrawal_id DESC LIMIT 20
        """)

    if not rows:
        await message.answer("No pending withdrawals.")
        return

    text = "💳 PENDING WITHDRAWALS\n\n"

    for row in rows:
        text += (
            f"ID: {row['withdrawal_id']}\n"
            f"User: {row['user_id']}\n"
            f"Amount: {row['amount']} points\n"
            f"Wallet: {row['wallet']}\n\n"
        )

    await message.answer(text)


@dp.message(Command("approvewithdraw"))
async def approve_withdrawal(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        withdrawal_id = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE withdrawals SET status = 'approved'
                WHERE withdrawal_id = $1 AND status = 'pending'
                RETURNING user_id, amount
            """, withdrawal_id)

        if not row:
            await message.answer("Pending request not found.")
            return

        await message.answer(
            f"✅ Withdrawal #{withdrawal_id} approved.\n"
            "Remember to send the crypto payment manually."
        )

        try:
            await bot.send_message(
                row["user_id"],
                f"✅ Your withdrawal request #{withdrawal_id} was approved."
            )
        except Exception:
            pass

    except (ValueError, IndexError):
        await message.answer("Use: /approvewithdraw ID")


@dp.message(Command("rejectwithdraw"))
async def reject_withdrawal(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        withdrawal_id = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE withdrawals SET status = 'rejected'
                    WHERE withdrawal_id = $1 AND status = 'pending'
                    RETURNING user_id, amount
                """, withdrawal_id)

                if row:
                    await conn.execute("""
                        UPDATE users SET points = points + $1
                        WHERE user_id = $2
                    """, row["amount"], row["user_id"])

        if not row:
            await message.answer("Pending request not found.")
            return

        await message.answer(
            "❌ Withdrawal rejected. Points refunded."
        )

        try:
            await bot.send_message(
                row["user_id"],
                "Your withdrawal was rejected. Points have been refunded."
            )
        except Exception:
            pass

    except (ValueError, IndexError):
        await message.answer("Use: /rejectwithdraw ID")


# ---------------- BROADCAST ----------------

@dp.message(Command("broadcast"))
async def broadcast(message: Message):
    if not await is_admin(message.from_user.id):
        return

    text = message.text.partition(" ")[2].strip()

    if not text:
        await message.answer("Use: /broadcast YOUR MESSAGE")
        return

    async with db_pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT user_id FROM users
            WHERE is_banned = FALSE
        """)

    sent = 0
    failed = 0

    for user in users:
        try:
            await bot.send_message(user["user_id"], text)
            sent += 1
        except Exception:
            failed += 1

    await message.answer(
        f"📢 Broadcast finished!\n"
        f"Sent: {sent}\nFailed: {failed}"
    )


# ---------------- REFERRAL ----------------

@dp.message(Command("referral"))
async def referral(message: Message):
    if not await allowed(message):
        return

    me = await bot.get_me()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        count = await conn.fetchval("""
            SELECT COUNT(*) FROM users WHERE referred_by = $1
        """, user_id)

    bonus = await get_setting("referral_bonus", REFERRAL_BONUS)
    link = f"https://t.me/{me.username}?start=ref_{user_id}"

    await message.answer(
        f"👥 Your referrals: {count}\n"
        f"🎁 Referral bonus: {bonus} points\n\n"
        f"Your referral link:\n{link}"
    )


# ---------------- SETTINGS ----------------

@dp.message(Command("setadreward"))
async def set_ad_reward(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        amount = int(message.text.split()[1])

        if amount < 0:
            raise ValueError

        await set_setting("ad_reward", amount)
        await message.answer(f"✅ Ad reward set to {amount} points.")

    except (ValueError, IndexError):
        await message.answer("Use: /setadreward AMOUNT")


@dp.message(Command("setminwithdraw"))
async def set_min_withdraw(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        amount = int(message.text.split()[1])

        if amount <= 0:
            raise ValueError

        await set_setting("min_withdraw", amount)
        await message.answer(
            f"✅ Minimum withdrawal set to {amount} points."
        )

    except (ValueError, IndexError):
        await message.answer("Use: /setminwithdraw AMOUNT")


@dp.message(Command("setrefbonus"))
async def set_ref_bonus(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        amount = int(message.text.split()[1])

        if amount < 0:
            raise ValueError

        await set_setting("referral_bonus", amount)
        await message.answer(
            f"✅ Referral bonus set to {amount} points."
        )

    except (ValueError, IndexError):
        await message.answer("Use: /setrefbonus AMOUNT")


# ---------------- USER MENU ----------------

@dp.message(lambda msg: msg.text == "🎬 Watch Ads")
async def watch_ads(message: Message):
    if not await allowed(message):
        return

    user_id = message.from_user.id
    reward = await get_setting("ad_reward", REWARD_POINTS)

    async with db_pool.acquire() as conn:
        result = await conn.fetchrow("""
            UPDATE users
            SET points = points + $2,
                last_ad_at = NOW()
            WHERE user_id = $1
            AND (
                last_ad_at IS NULL
                OR last_ad_at <= NOW() - INTERVAL '30 seconds'
            )
            RETURNING points
        """, user_id, reward)

        if result:
            await message.answer(
                f"🎉 You earned +{reward} points!\n"
                f"💰 Your balance: {result['points']} points"
            )
            return

        last_ad_at = await conn.fetchval(
            "SELECT last_ad_at FROM users WHERE user_id = $1",
            user_id
        )

    remaining = max(
        1,
        math.ceil(
            (
                last_ad_at
                + timedelta(seconds=COOLDOWN_SECONDS)
                - datetime.now(timezone.utc)
            ).total_seconds()
        )
    )

    await message.answer(
        f"⏳ Please wait {remaining}s before trying again."
    )


@dp.message(lambda msg: msg.text == "💰 Balance")
async def balance(message: Message):
    if not await allowed(message):
        return

    user = await get_user(message.from_user.id)

    await message.answer(
        f"💰 Your current balance: {user['points']} points"
    )


@dp.message(lambda msg: msg.text == "📋 Tasks")
async def tasks(message: Message):
    if not await allowed(message):
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT task_id, title, reward
            FROM tasks WHERE active = TRUE
            ORDER BY task_id DESC
        """)

    if not rows:
        await message.answer("No tasks available right now.")
        return

    text = "📋 AVAILABLE TASKS\n\n"

    for task in rows:
        text += (
            f"ID: {task['task_id']}\n"
            f"{task['title']}\n"
            f"Reward: {task['reward']} points\n\n"
        )

    text += "Submit proof using:\n/submit TASK_ID YOUR_PROOF"

    await message.answer(text)


@dp.message(lambda msg: msg.text == "💳 Withdraw")
async def withdraw_menu(message: Message):
    if not await allowed(message):
        return

    minimum = await get_setting("min_withdraw", MIN_WITHDRAWAL)

    await message.answer(
        f"💳 Minimum withdrawal: {minimum} points\n\n"
        "To request a withdrawal, send:\n"
        "/withdraw AMOUNT YOUR_CRYPTO_WALLET"
    )


# ---------------- FALLBACK ----------------

@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Please use the menu buttons below. 👇",
        reply_markup=keyboard
    )


# ---------------- WEB SERVER ----------------

async def health_handler(request: web.Request):
    return web.Response(text="EarnWave bot is running")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    return runner


async def main():
    await setup_database()
    runner = await start_web_server()

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await db_pool.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
