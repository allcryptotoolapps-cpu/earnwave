import aiohttp
import asyncio
import os
import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

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

# Set these in Render Environment before accepting advertiser deposits.
# Use separate addresses for each network. Never share private keys.
USDT_TRC20_ADDRESS = os.getenv("USDT_TRC20_ADDRESS", "")
USDT_BEP20_ADDRESS = os.getenv("USDT_BEP20_ADDRESS", "")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

bot = Bot(token=TOKEN)
dp = Dispatcher()
db_pool = None

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎬 Watch Ads")],
        [KeyboardButton(text="📋 Tasks"), KeyboardButton(text="💰 Balance")],
        [KeyboardButton(text="💳 Withdraw")],
        [KeyboardButton(text="📢 Advertise"), KeyboardButton(text="👛 Wallet")],
        [KeyboardButton(text="🌐 Language")]
    ],
    resize_keyboard=True
)


# ---------------- DATABASE ----------------

async def setup_database():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                points BIGINT NOT NULL DEFAULT 0,
                last_ad_at TIMESTAMPTZ,
                is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                referred_by BIGINT,
                referral_paid BOOLEAN NOT NULL DEFAULT FALSE,
                language TEXT NOT NULL DEFAULT 'en'
            )
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_paid BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en';
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

        # Advertiser wallet: amounts are USDT. Available and reserved are separate.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS advertiser_wallets (
                user_id BIGINT PRIMARY KEY,
                available NUMERIC(20,8) NOT NULL DEFAULT 0,
                reserved NUMERIC(20,8) NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS advertiser_transactions (
                tx_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                tx_type TEXT NOT NULL,
                amount NUMERIC(20,8) NOT NULL,
                network TEXT,
                reference TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                note TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS
    unique_advertiser_deposit_txid
    ON advertiser_transactions (LOWER(reference))
    WHERE tx_type = 'deposit' AND reference IS NOT NULL
""")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                campaign_id BIGSERIAL PRIMARY KEY,
                advertiser_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                channel_link TEXT NOT NULL,
                target_members INTEGER NOT NULL,
                reward_per_join NUMERIC(20,8) NOT NULL,
                total_budget NUMERIC(20,8) NOT NULL,
                spent NUMERIC(20,8) NOT NULL DEFAULT 0,
                verified_joins INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending_payment',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_joins (
                join_id BIGSERIAL PRIMARY KEY,
                campaign_id BIGINT NOT NULL REFERENCES campaigns(campaign_id),
                user_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(campaign_id, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS advertiser_withdrawals (
                withdrawal_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount NUMERIC(20,8) NOT NULL,
                network TEXT NOT NULL,
                wallet TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

    print("PostgreSQL connected successfully!")


async def get_setting(key, default):
    async with db_pool.acquire() as conn:
        value = await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return value


async def set_setting(key, value):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, key, str(value))


async def register_user(user_id, referred_by=None):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval("""
                INSERT INTO users (user_id, referred_by)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO NOTHING
                RETURNING user_id
            """, user_id, referred_by)
            if inserted:
                await conn.execute(
                    "INSERT INTO advertiser_wallets (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    user_id
                )
                if referred_by and referred_by != user_id:
                    referrer = await conn.fetchval(
                        "SELECT user_id FROM users WHERE user_id = $1", referred_by
                    )
                    if referrer:
                        bonus = await get_setting("referral_bonus", REFERRAL_BONUS)
                        await conn.execute(
                            "UPDATE users SET points = points + $1 WHERE user_id = $2",
                            bonus, referred_by
                        )
                        await conn.execute(
                            "UPDATE users SET referral_paid = TRUE WHERE user_id = $1",
                            user_id
                        )


async def get_user(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT points, last_ad_at, is_banned, language
            FROM users WHERE user_id = $1
        """, user_id)


async def is_admin(user_id):
    return user_id in ADMIN_IDS


async def allowed(message):
    if await is_admin(message.from_user.id):
        return True
    user = await get_user(message.from_user.id)
    if user and user["is_banned"]:
        await message.answer("⛔ Your account is banned.")
        return False
    return True



async def verify_deposit_on_blockchain(network, txid, amount):
    txid = txid.strip()

if network == "BEP20" and txid.lower().startswith("0x"):
    txid = txid[2:]

if len(txid) != 64 or any(c not in "0123456789abcdefABCDEF" for c in txid):
    return False

    if network == "TRC20":
        address = await get_setting(
            "deposit_address_TRC20", USDT_TRC20_ADDRESS
        )
        api_key = os.getenv("TRONGRID_API_KEY", "")

        if not address or not api_key:
            raise RuntimeError("TRON address or API key missing")

        expected = amount * Decimal(10 ** 6)
        if expected != expected.to_integral_value():
            return False

        url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
        params = {
            "only_confirmed": "true",
            "only_to": "true",
            "limit": 200,
            "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            "order_by": "block_timestamp,desc"
        }
        headers = {"TRON-PRO-API-KEY": api_key}

        async with aiohttp.ClientSession() as session:
            for _ in range(20):
                async with session.get(
                    url, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=25)
                ) as response:
                    response.raise_for_status()
                    result = await response.json()

                for item in result.get("data", []):
                    if (
                        item.get("transaction_id", "").lower() == txid.lower()
                        and item.get("to") == address
                        and item.get("token_info", {}).get("address")
                        == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
                        and int(item.get("value", "0")) == int(expected)
                    ):
                        return True

                fingerprint = result.get("meta", {}).get("fingerprint")
                if not fingerprint:
                    break
                params["fingerprint"] = fingerprint

        return False

    elif network == "BEP20":
        address = await get_setting(
            "deposit_address_BEP20", USDT_BEP20_ADDRESS
        )

        if not address or not address.startswith("0x") or len(address) != 42:
            raise RuntimeError("Invalid BEP20 deposit address")

        expected = amount * Decimal(10 ** 18)
        if expected != expected.to_integral_value():
            return False

        token = "0x55d398326f99059fF775485246999027B3197955"
        transfer_topic = (
            "0xddf252ad1be2c89b69c2b068fc378daa"
            "952ba7f163c4a11628f55a4df523b3ef"
        )

        async with aiohttp.ClientSession() as session:
            async def rpc(method, params):
                async with session.post(
                    "https://bsc-dataseed.binance.org/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params
                    },
                    timeout=aiohttp.ClientTimeout(total=25)
                ) as response:
                    response.raise_for_status()
                    data = await response.json()

                if "error" in data or "result" not in data:
                    raise RuntimeError("BSC RPC verification failed")
                return data["result"]

            chain_id = await rpc("eth_chainId", [])
            if chain_id != "0x38":
                return False

            receipt = await rpc(
                "eth_getTransactionReceipt", ["0x" + txid]
            )

            if not receipt or receipt.get("status") != "0x1":
                return False

            latest = await rpc("eth_blockNumber", [])
            confirmations = (
                int(latest, 16) - int(receipt["blockNumber"], 16)
            )

            if confirmations < 12:
                return False

            for log in receipt.get("logs", []):
                topics = log.get("topics", [])

                if (
                    log.get("address", "").lower() == token.lower()
                    and len(topics) >= 3
                    and topics[0].lower() == transfer_topic
                    and topics[2][-40:].lower() == address[2:].lower()
                    and int(log.get("data", "0x0"), 16) == int(expected)
                ):
                    return True

        return False

    return False



async def ensure_wallet(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO advertiser_wallets (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
            user_id
        )
        return await conn.fetchrow(
            "SELECT available, reserved FROM advertiser_wallets WHERE user_id = $1",
            user_id
        )


# ---------------- START / LANGUAGE ----------------

@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    args = (message.text or "").split()
    referred_by = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referred_by = int(args[1][4:])
        except ValueError:
            pass
    await register_user(user_id, referred_by)
    if not await allowed(message):
        return
    await message.answer(
        "Welcome to EarnWave! 🌍\n\n"
        "Earn points by completing available tasks. Advertisers can promote their channels.\n\n"
        "Use 🌐 Language to choose your language.",
        reply_markup=keyboard
    )


@dp.message(lambda m: m.text == "🌐 Language")
async def language_menu(message: Message):
    if not await allowed(message):
        return
    await message.answer(
        "Choose language:\n"
        "/lang en - English\n"
        "/lang bn - বাংলা\n"
        "/lang hi - हिन्दी\n"
        "/lang ur - اردو\n"
        "/lang ar - العربية\n"
        "/lang es - Español\n"
        "/lang fr - Français"
    )


@dp.message(Command("lang"))
async def set_language(message: Message):
    if not await allowed(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1] not in {"en", "bn", "hi", "ur", "ar", "es", "fr"}:
        await message.answer("Use: /lang en|bn|hi|ur|ar|es|fr")
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET language = $1 WHERE user_id = $2",
                           parts[1], message.from_user.id)
    await message.answer("✅ Language preference saved. Some messages are currently English while translations are being completed.")


# ---------------- ADMIN PANEL ----------------

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ Not authorized.")
        return
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_points = await conn.fetchval("SELECT COALESCE(SUM(points), 0) FROM users")
        pending_campaigns = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status = 'pending_payment'")
        pending_deposits = await conn.fetchval("SELECT COUNT(*) FROM advertiser_transactions WHERE tx_type = 'deposit' AND status = 'pending'")
        available_wallets = await conn.fetchval("SELECT COALESCE(SUM(available), 0) FROM advertiser_wallets")
    reward = await get_setting("ad_reward", REWARD_POINTS)
    minimum = await get_setting("min_withdraw", MIN_WITHDRAWAL)
    await message.answer(
        "🛠 EARNWAVE ADMIN PANEL\n\n"
        f"👥 Users: {total_users}\n💰 User Points: {total_points}\n"
        f"🎬 Test Ad Reward: {reward}\n💳 Minimum User Withdrawal: {minimum}\n"
        f"📢 Pending Campaigns: {pending_campaigns}\n"
        f"💵 Pending Deposits: {pending_deposits}\n"
        f"👛 Advertiser Available Wallets: {available_wallets} USDT\n\n"
        "Use /helpadmin for commands."
    )


@dp.message(Command("helpadmin"))
async def admin_help(message: Message):
    if not await is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 ADMIN COMMANDS\n\n"
        "/users\n/addpoints ID AMOUNT\n/removepoints ID AMOUNT\n/ban ID\n/unban ID\n"
        "/addtask REWARD | TASK NAME\n/tasklist\n/deltask TASK_ID\n"
        "/submissions\n/approve SUBMISSION_ID\n/reject SUBMISSION_ID\n"
        "/withdrawals\n/approvewithdraw ID\n/rejectwithdraw ID\n"
        "/broadcast MESSAGE\n/setadreward AMOUNT\n/setminwithdraw AMOUNT\n/setrefbonus AMOUNT\n"
        "/referral\n\n"
        "ADVERTISER:\n/deposits - pending deposit requests\n/approvedeposit TX_ID\n/rejectdeposit TX_ID\n"
        "/campaigns - pending campaigns\n/approvecampaign ID\n/rejectcampaign ID\n"
        "/approvejoin CAMPAIGN_ID USER_ID\n/rejectjoin CAMPAIGN_ID USER_ID\n"
        "/advertiserwithdrawals\n/approveadvwithdraw ID\n/rejectadvwithdraw ID\n"
        "/setdepositaddress NETWORK ADDRESS\n"
        "/setadvminwithdraw AMOUNT"
    )


# ---------------- USER MANAGEMENT ----------------

@dp.message(Command("users"))
async def admin_users(message: Message):
    if not await is_admin(message.from_user.id):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, points, is_banned FROM users ORDER BY points DESC LIMIT 20")
    if not rows:
        await message.answer("No users found.")
        return
    out = "👥 USER LIST\n\n"
    for row in rows:
        out += f"ID: {row['user_id']} | Points: {row['points']} | {'BANNED' if row['is_banned'] else 'ACTIVE'}\n"
    await message.answer(out)


@dp.message(Command("addpoints"))
async def add_points(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        _, uid, amount = message.text.split()
        uid, amount = int(uid), int(amount)
        if amount <= 0:
            raise ValueError
        async with db_pool.acquire() as conn:
            result = await conn.execute("UPDATE users SET points = points + $1 WHERE user_id = $2", amount, uid)
        await message.answer("User not found." if result == "UPDATE 0" else f"✅ Added {amount} points to {uid}.")
    except (ValueError, TypeError):
        await message.answer("Use: /addpoints USER_ID AMOUNT")


@dp.message(Command("removepoints"))
async def remove_points(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        _, uid, amount = message.text.split()
        uid, amount = int(uid), int(amount)
        if amount <= 0:
            raise ValueError
        async with db_pool.acquire() as conn:
            result = await conn.execute("UPDATE users SET points = GREATEST(points - $1, 0) WHERE user_id = $2", amount, uid)
        await message.answer("User not found." if result == "UPDATE 0" else f"✅ Removed {amount} points from {uid}.")
    except (ValueError, TypeError):
        await message.answer("Use: /removepoints USER_ID AMOUNT")


@dp.message(Command("ban"))
async def ban_user(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        uid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            result = await conn.execute("UPDATE users SET is_banned = TRUE WHERE user_id = $1", uid)
        if result == "UPDATE 0":
            await message.answer("User not found.")
        else:
            await message.answer(f"🚫 User {uid} banned.")
            try:
                await bot.send_message(uid, "⛔ Your account has been banned.")
            except Exception:
                pass
    except (ValueError, IndexError):
        await message.answer("Use: /ban USER_ID")


@dp.message(Command("unban"))
async def unban_user(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        uid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            result = await conn.execute("UPDATE users SET is_banned = FALSE WHERE user_id = $1", uid)
        await message.answer("User not found." if result == "UPDATE 0" else f"✅ User {uid} unbanned.")
    except (ValueError, IndexError):
        await message.answer("Use: /unban USER_ID")


# ---------------- TASKS / SUBMISSIONS ----------------

@dp.message(Command("addtask"))
async def add_task(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        data = message.text.split(maxsplit=1)[1]
        reward_text, title = data.split("|", 1)
        reward, title = int(reward_text.strip()), title.strip()
        if reward <= 0 or not title:
            raise ValueError
        async with db_pool.acquire() as conn:
            tid = await conn.fetchval("INSERT INTO tasks (title, reward) VALUES ($1, $2) RETURNING task_id", title, reward)
        await message.answer(f"✅ Task added!\nID: {tid}\nReward: {reward}\nTitle: {title}")
    except (ValueError, IndexError):
        await message.answer("Use: /addtask 50 | Join our Telegram channel")


@dp.message(Command("tasklist"))
async def task_list(message: Message):
    if not await is_admin(message.from_user.id):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT task_id, title, reward, active FROM tasks ORDER BY task_id DESC")
    if not rows:
        await message.answer("No tasks available.")
        return
    out = "📋 TASK LIST\n\n"
    for r in rows:
        out += f"ID: {r['task_id']}\n{r['title']}\nReward: {r['reward']} | {'Active' if r['active'] else 'Deleted'}\n\n"
    await message.answer(out)


@dp.message(Command("deltask"))
async def delete_task(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        tid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            result = await conn.execute("UPDATE tasks SET active = FALSE WHERE task_id = $1", tid)
        await message.answer("Task not found." if result == "UPDATE 0" else f"🗑 Task {tid} deleted.")
    except (ValueError, IndexError):
        await message.answer("Use: /deltask TASK_ID")


@dp.message(Command("submit"))
async def submit_task(message: Message):
    if not await allowed(message):
        return
    try:
        _, tid, proof = message.text.split(maxsplit=2)
        tid = int(tid)
        async with db_pool.acquire() as conn:
            task = await conn.fetchval("SELECT task_id FROM tasks WHERE task_id = $1 AND active = TRUE", tid)
            if not task:
                await message.answer("Task not found or inactive.")
                return
            existing = await conn.fetchval("""
                SELECT submission_id FROM submissions
                WHERE user_id = $1 AND task_id = $2 AND status IN ('pending', 'approved')
            """, message.from_user.id, tid)
            if existing:
                await message.answer("You already submitted this task.")
                return
            sid = await conn.fetchval("""
                INSERT INTO submissions (user_id, task_id, proof) VALUES ($1, $2, $3) RETURNING submission_id
            """, message.from_user.id, tid, proof)
        await message.answer(f"✅ Submission received!\nSubmission ID: {sid}")
    except (ValueError, IndexError):
        await message.answer("Use: /submit TASK_ID YOUR_PROOF")


@dp.message(Command("submissions"))
async def pending_submissions(message: Message):
    if not await is_admin(message.from_user.id):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT submission_id, user_id, task_id, proof FROM submissions WHERE status = 'pending' ORDER BY submission_id DESC LIMIT 20")
    if not rows:
        await message.answer("No pending submissions.")
        return
    out = "📨 PENDING SUBMISSIONS\n\n"
    for r in rows:
        out += f"Submission: {r['submission_id']}\nUser: {r['user_id']}\nTask: {r['task_id']}\nProof: {r['proof'][:250]}\n\n"
    await message.answer(out)


@dp.message(Command("approve"))
async def approve_submission(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        sid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT s.user_id, t.reward FROM submissions s
                    JOIN tasks t ON t.task_id = s.task_id
                    WHERE s.submission_id = $1 AND s.status = 'pending'
                    FOR UPDATE OF s
                """, sid)
                if not row:
                    await message.answer("Pending submission not found.")
                    return
                await conn.execute("UPDATE submissions SET status = 'approved' WHERE submission_id = $1", sid)
                await conn.execute("UPDATE users SET points = points + $1 WHERE user_id = $2", row["reward"], row["user_id"])
        await message.answer("✅ Submission approved and reward added.")
        try:
            await bot.send_message(row["user_id"], f"🎉 Your task was approved! +{row['reward']} points.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /approve SUBMISSION_ID")


@dp.message(Command("reject"))
async def reject_submission(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        sid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE submissions SET status = 'rejected'
                WHERE submission_id = $1 AND status = 'pending' RETURNING user_id
            """, sid)
        if not row:
            await message.answer("Pending submission not found.")
            return
        await message.answer("❌ Submission rejected.")
        try:
            await bot.send_message(row["user_id"], "Your task submission was rejected.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /reject SUBMISSION_ID")


# ---------------- USER WITHDRAWALS ----------------


@dp.message(Command("withdraw"))
async def create_withdrawal(message: Message):
    if not await allowed(message):
        return

    try:
        _, amount_text, network, wallet = message.text.split(maxsplit=3)
        amount = int(amount_text)
        network = network.upper().strip()
        wallet = wallet.strip()

        if network not in ("TRC20", "BEP20"):
            await message.answer("Network must be TRC20 or BEP20.")
            return

        uid = message.from_user.id
        minimum = await get_setting("min_withdraw", MIN_WITHDRAWAL)

        if amount < minimum or amount <= 0:
            await message.answer(
                f"Minimum withdrawal is {minimum} points ($5 USDT)."
            )
            return

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                user = await conn.fetchrow(
                    "SELECT points FROM users WHERE user_id = $1 FOR UPDATE",
                    uid
                )

                if not user or user["points"] < amount:
                    await message.answer("Insufficient points.")
                    return

                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    amount, uid
                )

                wid = await conn.fetchval("""
                    INSERT INTO withdrawals (user_id, amount, wallet)
                    VALUES ($1, $2, $3)
                    RETURNING withdrawal_id
                """, uid, amount, f"{network}|{wallet}")

        await message.answer(
            f"✅ Withdrawal request submitted!\n"
            f"Request ID: {wid}\n"
            f"Amount: {amount} points (${amount / 1000:.2f} USDT)\n"
            f"Network: {network}\n"
            f"Points reserved pending review."
        )

    except (ValueError, IndexError):
        await message.answer(
            "Use:\n/withdraw AMOUNT TRC20 YOUR_WALLET\n"
            "or\n/withdraw AMOUNT BEP20 YOUR_WALLET"
        )



@dp.message(Command("withdrawals"))
async def pending_withdrawals(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        parts = message.text.split(maxsplit=1)

        status_filter = "all"
        if len(parts) > 1:
            status_filter = parts[1].lower()

        allowed_statuses = (
            "all", "pending", "approved", "paid", "rejected"
        )

        if status_filter not in allowed_statuses:
            await message.answer(
                "Use:\n"
                "/withdrawals\n"
                "/withdrawals pending\n"
                "/withdrawals approved\n"
                "/withdrawals paid\n"
                "/withdrawals rejected"
            )
            return

        async with db_pool.acquire() as conn:
            if status_filter == "all":
                rows = await conn.fetch("""
                    SELECT withdrawal_id, user_id, amount,
                           wallet, status, txid
                    FROM withdrawals
                    ORDER BY withdrawal_id DESC
                    LIMIT 30
                """)
            else:
                rows = await conn.fetch("""
                    SELECT withdrawal_id, user_id, amount,
                           wallet, status, txid
                    FROM withdrawals
                    WHERE status = $1
                    ORDER BY withdrawal_id DESC
                    LIMIT 30
                """, status_filter)

        if not rows:
            await message.answer(
                f"No {status_filter} withdrawals found."
            )
            return

        out = f"💳 WITHDRAWALS: {status_filter.upper()}\n\n"

        for r in rows:
            saved_wallet = r["wallet"]

            if "|" in saved_wallet:
                network, wallet = saved_wallet.split("|", 1)
            else:
                network, wallet = "OLD REQUEST", saved_wallet

            out += (
                f"ID: {r['withdrawal_id']}\n"
                f"User: {r['user_id']}\n"
                f"Amount: {r['amount']} points "
                f"(${r['amount'] / 1000:.2f} USDT)\n"
                f"Network: {network}\n"
                f"Wallet: {wallet}\n"
                f"Status: {r['status'].upper()}\n"
                f"TXID: {r['txid'] or 'Not available'}\n\n"
            )

        await message.answer(out)

    except Exception as e:
        await message.answer(f"Error: {str(e)}")






@dp.message(Command("approvewithdraw"))
async def approve_withdrawal(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        wid = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE withdrawals
                SET status = 'approved'
                WHERE withdrawal_id = $1 AND status = 'pending'
                RETURNING user_id, amount, wallet
            """, wid)

        if not row:
            await message.answer("Pending request not found.")
            return

        saved_wallet = row["wallet"]

        if "|" in saved_wallet:
            network, wallet = saved_wallet.split("|", 1)
        else:
            network, wallet = "OLD REQUEST", saved_wallet

        usdt = row["amount"] / 1000

        await message.answer(
            f"✅ Withdrawal #{wid} approved.\n\n"
            f"Amount: ${usdt:.2f} USDT\n"
            f"Network: {network}\n"
            f"Wallet: {wallet}\n\n"
            f"After sending crypto, submit:\n"
            f"/markpaid {wid} TXID"
        )

        try:
            await bot.send_message(
                row["user_id"],
                f"⏳ Your withdrawal #{wid} has been approved. "
                f"Payment is being processed."
            )
        except Exception:
            pass

    except (ValueError, IndexError):
        await message.answer("Use: /approvewithdraw ID")



@dp.message(Command("markpaid"))
async def mark_withdrawal_paid(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        _, wid_text, txid = message.text.split(maxsplit=2)
        wid = int(wid_text)
        txid = txid.strip()

        if not txid:
            await message.answer("TXID is required.")
            return

        async with db_pool.acquire() as conn:
            await conn.execute("""
                ALTER TABLE withdrawals
                ADD COLUMN IF NOT EXISTS txid TEXT
            """)

            row = await conn.fetchrow("""
                UPDATE withdrawals
                SET status = 'paid', txid = $2
                WHERE withdrawal_id = $1 AND status = 'approved'
                RETURNING user_id, amount
            """, wid, txid)

        if not row:
            await message.answer(
                "Approved withdrawal not found or already paid."
            )
            return

        await message.answer(
            f"✅ Withdrawal #{wid} marked as PAID.\n"
            f"TXID: {txid}"
        )

        try:
            await bot.send_message(
                row["user_id"],
                f"✅ Your withdrawal #{wid} has been PAID!\n"
                f"Amount: ${row['amount'] / 1000:.2f} USDT\n"
                f"TXID: {txid}"
            )
        except Exception:
            pass

    except (ValueError, IndexError):
        await message.answer("Use: /markpaid ID TXID")


@dp.message(Command("mywithdrawals"))
async def my_withdrawals(message: Message):
    uid = message.from_user.id

    async with db_pool.acquire() as conn:
        await conn.execute("""
            ALTER TABLE withdrawals
            ADD COLUMN IF NOT EXISTS txid TEXT
        """)

        rows = await conn.fetch("""
            SELECT withdrawal_id, amount, wallet, status, txid
            FROM withdrawals
            WHERE user_id = $1
            ORDER BY withdrawal_id DESC
            LIMIT 10
        """, uid)

    if not rows:
        await message.answer("You have no withdrawal history.")
        return

    out = "📋 YOUR WITHDRAWAL HISTORY\n\n"

    for r in rows:
        saved_wallet = r["wallet"]

        if "|" in saved_wallet:
            network, wallet = saved_wallet.split("|", 1)
        else:
            network, wallet = "OLD REQUEST", saved_wallet

        out += (
            f"ID: {r['withdrawal_id']}\n"
            f"Amount: ${r['amount'] / 1000:.2f} USDT\n"
            f"Network: {network}\n"
            f"Status: {r['status'].upper()}\n"
            f"TXID: {r['txid'] or 'Not available'}\n\n"
        )

    await message.answer(out)



@dp.message(Command("rejectwithdraw"))
async def reject_withdrawal(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        wid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE withdrawals SET status = 'rejected'
                    WHERE withdrawal_id = $1 AND status = 'pending'
                    RETURNING user_id, amount
                """, wid)
                if row:
                    await conn.execute("UPDATE users SET points = points + $1 WHERE user_id = $2", row["amount"], row["user_id"])
        if not row:
            await message.answer("Pending request not found.")
            return
        await message.answer("❌ Withdrawal rejected. Points refunded.")
        try:
            await bot.send_message(row["user_id"], "Your withdrawal was rejected. Points have been refunded.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /rejectwithdraw ID")


# ---------------- REFERRAL / SETTINGS ----------------

@dp.message(Command("referral"))
async def referral(message: Message):
    if not await allowed(message):
        return
    me = await bot.get_me()
    uid = message.from_user.id
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by = $1", uid)
    bonus = await get_setting("referral_bonus", REFERRAL_BONUS)
    await message.answer(f"👥 Your referrals: {count}\n🎁 Referral bonus: {bonus} points\n\nYour referral link:\nhttps://t.me/{me.username}?start=ref_{uid}")


@dp.message(Command("setadreward"))
async def set_ad_reward(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        amount = int(message.text.split()[1])
        if amount < 0:
            raise ValueError
        await set_setting("ad_reward", amount)
        await message.answer(f"✅ Test ad reward set to {amount} points.")
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
        await message.answer(f"✅ Minimum withdrawal set to {amount} points.")
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
        await message.answer(f"✅ Referral bonus set to {amount} points.")
    except (ValueError, IndexError):
        await message.answer("Use: /setrefbonus AMOUNT")


@dp.message(Command("broadcast"))
async def broadcast(message: Message):
    if not await is_admin(message.from_user.id):
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.answer("Use: /broadcast YOUR MESSAGE")
        return
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE")
    sent = failed = 0
    for u in users:
        try:
            await bot.send_message(u["user_id"], text)
            sent += 1
        except Exception:
            failed += 1
    await message.answer(f"📢 Broadcast finished!\nSent: {sent}\nFailed: {failed}")


# ---------------- ADVERTISER WALLET ----------------

@dp.message(lambda m: m.text in ("👛 Wallet", "/wallet"))
async def advertiser_wallet(message: Message):
    if not await allowed(message):
        return
    w = await ensure_wallet(message.from_user.id)
    await message.answer(
        "👛 ADVERTISER WALLET\n\n"
        f"Available: {Decimal(w['available']):.8f} USDT\n"
        f"Reserved for campaigns: {Decimal(w['reserved']):.8f} USDT\n\n"
        "Use /deposit to add USDT.\n"
        "Use /advwithdraw AMOUNT NETWORK WALLET to withdraw available funds.\n"
        "Use /transactions to view history."
    )


@dp.message(Command("deposit"))
async def advertiser_deposit(message: Message):
    if not await allowed(message):
        return
    await ensure_wallet(message.from_user.id)
    trc20 = await get_setting("deposit_address_TRC20", USDT_TRC20_ADDRESS)
    bep20 = await get_setting("deposit_address_BEP20", USDT_BEP20_ADDRESS)
    await message.answer(
        "💵 USDT DEPOSIT\n\n"
        f"TRC20 address: {trc20 or 'Not configured yet'}\n"
        f"BEP20 address: {bep20 or 'Not configured yet'}\n\n"
        "Send USDT using the exact matching network. Network fee is paid by you.\n"
        "After sending, submit:\n"
        "/depositproof AMOUNT NETWORK TXID\n\n"
        "Example: /depositproof 25 TRC20 transaction_hash\n"
        "Your balance is credited only after admin verifies the transaction on the blockchain."
    )


@dp.message(Command("depositproof"))
async def deposit_proof(message: Message):
    if not await allowed(message):
        return
    try:
        _, amount_text, network, txid = message.text.split(maxsplit=3)
        amount = parse_usdt(amount_text)
        network = network.upper()
        if network not in {"TRC20", "BEP20"} or len(txid) < 10:
            raise ValueError
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval("SELECT tx_id FROM advertiser_transactions WHERE reference = $1", txid)
            if exists:
                await message.answer("This transaction ID has already been submitted.")
                return
            tx = await conn.fetchval("""
                INSERT INTO advertiser_transactions (user_id, tx_type, amount, network, reference, note)
                VALUES ($1, 'deposit', $2, $3, $4, 'Advertiser deposit proof')
                RETURNING tx_id
            """, message.from_user.id, amount, network, txid)
        await message.answer(f"✅ Deposit proof submitted. Request ID: {tx}\nYour wallet will be credited after admin verification.")
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"💵 Deposit proof #{tx}\nUser: {message.from_user.id}\nAmount: {amount} USDT\nNetwork: {network}\nTXID: {txid}\nReview with /deposits")
            except Exception:
                pass
    except (ValueError, IndexError):
        await message.answer("Use: /depositproof AMOUNT TRC20|BEP20 TXID")


@dp.message(Command("transactions"))
async def advertiser_transactions(message: Message):
    if not await allowed(message):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tx_id, tx_type, amount, network, status, reference, created_at
            FROM advertiser_transactions WHERE user_id = $1
            ORDER BY tx_id DESC LIMIT 15
        """, message.from_user.id)
    if not rows:
        await message.answer("No advertiser transactions yet.")
        return
    out = "📜 ADVERTISER TRANSACTIONS\n\n"
    for r in rows:
        out += f"#{r['tx_id']} {r['tx_type']} {r['amount']} USDT | {r['network'] or '-'} | {r['status']}\n"
    await message.answer(out)


@dp.message(Command("deposits"))
async def admin_deposits(message: Message):
    if not await is_admin(message.from_user.id):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tx_id, user_id, amount, network, reference
            FROM advertiser_transactions
            WHERE tx_type = 'deposit' AND status = 'pending'
            ORDER BY tx_id DESC LIMIT 20
        """)
    if not rows:
        await message.answer("No pending deposits.")
        return
    out = "💵 PENDING ADVERTISER DEPOSITS\n\n"
    for r in rows:
        out += f"ID: {r['tx_id']} | User: {r['user_id']}\nAmount: {r['amount']} USDT | {r['network']}\nTXID: {r['reference']}\n\n"
    await message.answer(out)



@dp.message(Command("approvedeposit"))
async def approve_deposit(message: Message):
    if not await is_admin(message.from_user.id):
        return

    try:
        txid = int(message.text.split()[1])

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM advertiser_transactions
                WHERE tx_id = $1
                AND tx_type = 'deposit'
                AND status = 'pending'
            """, txid)

        if not row:
            await message.answer("Pending deposit not found.")
            return

        # Verify actual blockchain transaction
        try:
            verified = await verify_deposit_on_blockchain(
                row["network"],
                row["reference"],
                row["amount"]
            )
        except Exception as e:
            await message.answer(
                f"Blockchain verification error: {str(e)[:250]}\n"
                "No balance was credited."
            )
            return

        if not verified:
            await message.answer(
                "❌ Blockchain verification failed.\n"
                "No balance was credited."
            )
            return

        async with db_pool.acquire() as conn:
            async with conn.transaction():

                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    row["reference"].lower()
                )

                current = await conn.fetchrow("""
                    SELECT * FROM advertiser_transactions
                    WHERE tx_id = $1
                    AND tx_type = 'deposit'
                    AND status = 'pending'
                    FOR UPDATE
                """, txid)

                if not current:
                    await message.answer(
                        "Deposit already processed."
                    )
                    return

                duplicate = await conn.fetchval("""
                    SELECT EXISTS(
                        SELECT 1 FROM advertiser_transactions
                        WHERE tx_type = 'deposit'
                        AND LOWER(reference) = LOWER($1)
                        AND status = 'approved'
                        AND tx_id != $2
                    )
                """, row["reference"], txid)

                if duplicate:
                    await message.answer(
                        "This transaction was already credited."
                    )
                    return

                await conn.execute("""
                    UPDATE advertiser_transactions
                    SET status = 'approved'
                    WHERE tx_id = $1
                """, txid)

                await conn.execute("""
                    INSERT INTO advertiser_wallets (user_id, available)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE
                    SET available = advertiser_wallets.available
                        + EXCLUDED.available,
                        updated_at = NOW()
                """, row["user_id"], row["amount"])

        await message.answer(
            f"✅ Deposit #{txid} verified and approved.\n"
            f"Wallet credited: {row['amount']} USDT."
        )

        try:
            await bot.send_message(
                row["user_id"],
                f"✅ Your deposit #{txid} was verified.\n"
                f"{row['amount']} USDT added to your Advertiser Wallet."
            )
        except Exception:
            pass

    except (ValueError, IndexError):
        await message.answer("Use: /approvedeposit TX_ID")



@dp.message(Command("rejectdeposit"))
async def reject_deposit(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        txid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE advertiser_transactions SET status = 'rejected'
                WHERE tx_id = $1 AND tx_type = 'deposit' AND status = 'pending'
                RETURNING user_id
            """, txid)
        if not row:
            await message.answer("Pending deposit not found.")
            return
        await message.answer(f"❌ Deposit #{txid} rejected.")
        try:
            await bot.send_message(row["user_id"], f"Your deposit request #{txid} was rejected. Contact support if you believe this is an error.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /rejectdeposit TX_ID")


@dp.message(Command("setdepositaddress"))
async def set_deposit_address(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        _, network, address = message.text.split(maxsplit=2)
        network = network.upper()
        if network not in {"TRC20", "BEP20"} or len(address) < 20:
            raise ValueError
        key = "deposit_address_" + network
        await set_setting(key, address)
        await message.answer(f"✅ {network} deposit address saved in database.")
    except (ValueError, IndexError):
        await message.answer("Use: /setdepositaddress TRC20|BEP20 ADDRESS")


# ---------------- ADVERTISER WITHDRAWALS ----------------

@dp.message(Command("advwithdraw"))
async def advertiser_withdraw(message: Message):
    if not await allowed(message):
        return
    try:
        _, amount_text, network, wallet = message.text.split(maxsplit=3)
        amount = parse_usdt(amount_text)
        network = network.upper()
        if network not in {"TRC20", "BEP20"} or len(wallet) < 20:
            raise ValueError
        minimum = Decimal(str(await get_setting("adv_min_withdraw", "10")))
        if amount < minimum:
            await message.answer(f"Minimum advertiser withdrawal is {minimum} USDT.")
            return
        uid = message.from_user.id
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                w = await conn.fetchrow("SELECT available FROM advertiser_wallets WHERE user_id = $1 FOR UPDATE", uid)
                if not w or Decimal(w["available"]) < amount:
                    await message.answer("Insufficient available balance. Reserved campaign funds cannot be withdrawn.")
                    return
                await conn.execute("UPDATE advertiser_wallets SET available = available - $1, updated_at = NOW() WHERE user_id = $2", amount, uid)
                wid = await conn.fetchval("""
                    INSERT INTO advertiser_withdrawals (user_id, amount, network, wallet)
                    VALUES ($1, $2, $3, $4) RETURNING withdrawal_id
                """, uid, amount, network, wallet)
                await conn.execute("""
                    INSERT INTO advertiser_transactions (user_id, tx_type, amount, network, reference, status, note)
                    VALUES ($1, 'withdrawal', $2, $3, $4, 'pending', $5)
                """, uid, amount, network, wallet, f"Withdrawal request #{wid}")
        await message.answer(f"✅ Withdrawal request #{wid} submitted.\nAmount: {amount} USDT\nNetwork fee is deducted from the payout, not your requested wallet balance. Admin will process it manually.")
    except (ValueError, IndexError):
        await message.answer("Use: /advwithdraw AMOUNT TRC20|BEP20 WALLET_ADDRESS")


@dp.message(Command("advertiserwithdrawals"))
async def admin_adv_withdrawals(message: Message):
    if not await is_admin(message.from_user.id):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT withdrawal_id, user_id, amount, network, wallet FROM advertiser_withdrawals WHERE status = 'pending' ORDER BY withdrawal_id DESC LIMIT 20")
    if not rows:
        await message.answer("No pending advertiser withdrawals.")
        return
    out = "👛 ADVERTISER WITHDRAWALS\n\n"
    for r in rows:
        out += f"ID: {r['withdrawal_id']} | User: {r['user_id']}\n{r['amount']} USDT {r['network']}\nWallet: {r['wallet']}\n\n"
    await message.answer(out)


@dp.message(Command("approveadvwithdraw"))
async def approve_adv_withdraw(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        wid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE advertiser_withdrawals SET status = 'approved'
                    WHERE withdrawal_id = $1 AND status = 'pending'
                    RETURNING user_id, amount, network
                """, wid)
                if not row:
                    await message.answer("Pending withdrawal not found.")
                    return
                await conn.execute("""
                    UPDATE advertiser_transactions SET status = 'approved'
                    WHERE user_id = $1 AND tx_type = 'withdrawal' AND note = $2 AND status = 'pending'
                """, row["user_id"], f"Withdrawal request #{wid}")
        await message.answer(f"✅ Advertiser withdrawal #{wid} marked approved. Send the crypto manually.")
        try:
            await bot.send_message(row["user_id"], f"Your advertiser withdrawal #{wid} has been approved. Please check your wallet after the manual transfer.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /approveadvwithdraw ID")


@dp.message(Command("rejectadvwithdraw"))
async def reject_adv_withdraw(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        wid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE advertiser_withdrawals SET status = 'rejected'
                    WHERE withdrawal_id = $1 AND status = 'pending'
                    RETURNING user_id, amount, network
                """, wid)
                if not row:
                    await message.answer("Pending withdrawal not found.")
                    return
                await conn.execute("UPDATE advertiser_wallets SET available = available + $1, updated_at = NOW() WHERE user_id = $2", row["amount"], row["user_id"])
                await conn.execute("""
                    UPDATE advertiser_transactions SET status = 'rejected'
                    WHERE user_id = $1 AND tx_type = 'withdrawal' AND note = $2 AND status = 'pending'
                """, row["user_id"], f"Withdrawal request #{wid}")
        await message.answer(f"❌ Advertiser withdrawal #{wid} rejected. Funds refunded to available wallet.")
        try:
            await bot.send_message(row["user_id"], f"Your advertiser withdrawal #{wid} was rejected. Funds have been returned to your wallet.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /rejectadvwithdraw ID")


@dp.message(Command("setadvminwithdraw"))
async def set_adv_min_withdraw(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        amount = parse_usdt(message.text.split()[1])
        await set_setting("adv_min_withdraw", str(amount))
        await message.answer(f"✅ Advertiser minimum withdrawal set to {amount} USDT.")
    except (ValueError, IndexError):
        await message.answer("Use: /setadvminwithdraw AMOUNT")


# ---------------- ADVERTISER CAMPAIGNS ----------------

@dp.message(lambda m: m.text in ("📢 Advertise", "/advertise"))
async def advertise_menu(message: Message):
    if not await allowed(message):
        return
    await message.answer(
        "📢 ADVERTISER CENTER\n\n"
        "Create a promotion request with:\n"
        "/createcampaign TITLE | CHANNEL_LINK | TARGET_MEMBERS | REWARD_PER_JOIN_USDT\n\n"
        "Example:\n/createcampaign My Channel | https://t.me/example | 100 | 0.05\n\n"
        "The total budget is calculated as target × reward per verified join.\n"
        "Your wallet must have enough available USDT. Campaigns require admin approval before going live.\n\n"
        "Use /mycampaigns to view your campaigns and /wallet for your balance."
    )


@dp.message(Command("createcampaign"))
async def create_campaign(message: Message):
    if not await allowed(message):
        return
    try:
        raw = message.text.split(maxsplit=1)[1]
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 4:
            raise ValueError
        title, link, target_text, reward_text = parts
        target = int(target_text)
        reward = parse_usdt(reward_text)
        if not title or not link.startswith("https://t.me/") or target < 1 or target > 100000:
            raise ValueError
        budget = (reward * target).quantize(Decimal("0.00000001"))
        if budget < Decimal("1"):
            await message.answer("Campaign budget must be at least 1 USDT.")
            return
        uid = message.from_user.id
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                wallet = await conn.fetchrow("SELECT available FROM advertiser_wallets WHERE user_id = $1 FOR UPDATE", uid)
                if not wallet or Decimal(wallet["available"]) < budget:
                    await message.answer(f"Insufficient available balance. Required: {budget} USDT. Use /deposit first.")
                    return
                await conn.execute("UPDATE advertiser_wallets SET available = available - $1, reserved = reserved + $1, updated_at = NOW() WHERE user_id = $2", budget, uid)
                cid = await conn.fetchval("""
                    INSERT INTO campaigns (advertiser_id, title, channel_link, target_members, reward_per_join, total_budget)
                    VALUES ($1, $2, $3, $4, $5, $6) RETURNING campaign_id
                """, uid, title, link, target, reward, budget)
        await message.answer(
            f"✅ Campaign request #{cid} created.\n"
            f"Target: {target} joins\nReward per verified join: {reward} USDT\n"
            f"Reserved budget: {budget} USDT\nStatus: Pending admin review.\n"
            "The campaign is not public until approved."
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"📢 New campaign #{cid}\nAdvertiser: {uid}\nTitle: {title}\nLink: {link}\nTarget: {target}\nReward/join: {reward} USDT\nBudget: {budget} USDT\nReview: /campaigns")
            except Exception:
                pass
    except (ValueError, IndexError):
        await message.answer("Use:\n/createcampaign TITLE | https://t.me/channel | TARGET_MEMBERS | REWARD_PER_JOIN_USDT")


@dp.message(Command("mycampaigns"))
async def my_campaigns(message: Message):
    if not await allowed(message):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT campaign_id, title, target_members, verified_joins, total_budget, spent, status
            FROM campaigns WHERE advertiser_id = $1 ORDER BY campaign_id DESC LIMIT 20
        """, message.from_user.id)
    if not rows:
        await message.answer("You have no campaigns yet.")
        return
    out = "📢 YOUR CAMPAIGNS\n\n"
    for r in rows:
        progress = (r["verified_joins"] / r["target_members"] * 100) if r["target_members"] else 0
        out += f"#{r['campaign_id']} {r['title']}\nStatus: {r['status']}\nProgress: {r['verified_joins']}/{r['target_members']} ({progress:.1f}%)\nSpent: {r['spent']} / {r['total_budget']} USDT\n\n"
    await message.answer(out)


@dp.message(Command("campaigns"))
async def admin_campaigns(message: Message):
    if not await is_admin(message.from_user.id):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT campaign_id, advertiser_id, title, channel_link, target_members,
                   reward_per_join, total_budget, verified_joins, status
            FROM campaigns WHERE status IN ('pending_payment', 'pending_review', 'active')
            ORDER BY campaign_id DESC LIMIT 20
        """)
    if not rows:
        await message.answer("No campaigns awaiting review.")
        return
    out = "📢 CAMPAIGNS\n\n"
    for r in rows:
        out += f"#{r['campaign_id']} | Advertiser: {r['advertiser_id']}\n{r['title']}\n{r['channel_link']}\nTarget: {r['target_members']} | Joined: {r['verified_joins']}\nReward: {r['reward_per_join']} USDT | Budget: {r['total_budget']}\nStatus: {r['status']}\nApprove: /approvecampaign {r['campaign_id']}\nReject: /rejectcampaign {r['campaign_id']}\n\n"
    await message.answer(out)


@dp.message(Command("approvecampaign"))
async def approve_campaign(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        cid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE campaigns SET status = 'active', updated_at = NOW()
                WHERE campaign_id = $1 AND status = 'pending_payment'
                RETURNING advertiser_id, title
            """, cid)
        if not row:
            await message.answer("Pending campaign not found.")
            return
        await message.answer(f"✅ Campaign #{cid} approved and active.")
        try:
            await bot.send_message(row["advertiser_id"], f"Your campaign #{cid} ({row['title']}) is approved. Note: join verification must be performed by an admin until bot membership checking is configured.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /approvecampaign ID")


@dp.message(Command("rejectcampaign"))
async def reject_campaign(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        cid = int(message.text.split()[1])
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE campaigns SET status = 'rejected', updated_at = NOW()
                    WHERE campaign_id = $1 AND status IN ('pending_payment', 'pending_review')
                    RETURNING advertiser_id, total_budget
                """, cid)
                if row:
                    await conn.execute("""
                        UPDATE advertiser_wallets
                        SET available = available + $1,
                            reserved = GREATEST(reserved - $1, 0),
                            updated_at = NOW()
                        WHERE user_id = $2
                    """, row["total_budget"], row["advertiser_id"])
        if not row:
            await message.answer("Campaign not found.")
            return
        await message.answer(f"❌ Campaign #{cid} rejected. Reserved budget refunded.")
        try:
            await bot.send_message(row["advertiser_id"], f"Your campaign #{cid} was rejected. Reserved budget was returned to your available wallet.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /rejectcampaign ID")


@dp.message(Command("joincampaign"))
async def join_campaign(message: Message):
    if not await allowed(message):
        return
    try:
        cid = int(message.text.split()[1])
        uid = message.from_user.id
        async with db_pool.acquire() as conn:
            campaign = await conn.fetchrow("""
                SELECT campaign_id, title, channel_link, target_members, verified_joins
                FROM campaigns WHERE campaign_id = $1 AND status = 'active'
            """, cid)
            if not campaign:
                await message.answer("Campaign not found or not active.")
                return
            if campaign["verified_joins"] >= campaign["target_members"]:
                await message.answer("This campaign has reached its target.")
                return
            inserted = await conn.fetchval("""
                INSERT INTO campaign_joins (campaign_id, user_id)
                VALUES ($1, $2) ON CONFLICT (campaign_id, user_id) DO NOTHING
                RETURNING join_id
            """, cid, uid)
        if not inserted:
            await message.answer("You have already joined/submitted this campaign.")
            return
        await message.answer(
            f"Campaign: {campaign['title']}\nJoin here: {campaign['channel_link']}\n\n"
            f"After joining, send proof with:\n/campaignproof {cid} YOUR_PROOF\n"
            "Reward is credited only after verification."
        )
    except (ValueError, IndexError):
        await message.answer("Use: /joincampaign CAMPAIGN_ID")


@dp.message(Command("campaignproof"))
async def campaign_proof(message: Message):
    if not await allowed(message):
        return
    try:
        _, cid_text, proof = message.text.split(maxsplit=2)
        cid = int(cid_text)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE campaign_joins SET status = 'pending'
                WHERE campaign_id = $1 AND user_id = $2 AND status = 'pending'
                RETURNING join_id
            """, cid, message.from_user.id)
            if not row:
                await message.answer("No join request found. First use /joincampaign ID.")
                return
            await conn.execute("""
                INSERT INTO advertiser_transactions (user_id, tx_type, amount, network, reference, status, note)
                VALUES ($1, 'joinproof', 0, NULL, $2, 'pending', $3)
            """, message.from_user.id, proof[:250], f"Campaign {cid}")
        await message.answer("✅ Proof submitted. Admin will verify your membership before rewarding you.")
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"Join proof pending\nCampaign: {cid}\nUser: {message.from_user.id}\nProof: {proof[:250]}\nUse /approvejoin {cid} {message.from_user.id} or /rejectjoin {cid} {message.from_user.id}")
            except Exception:
                pass
    except (ValueError, IndexError):
        await message.answer("Use: /campaignproof CAMPAIGN_ID YOUR_PROOF")


@dp.message(Command("approvejoin"))
async def approve_join(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        _, cid_text, uid_text = message.text.split()
        cid, uid = int(cid_text), int(uid_text)
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                campaign = await conn.fetchrow("""
                    SELECT * FROM campaigns WHERE campaign_id = $1 AND status = 'active' FOR UPDATE
                """, cid)
                join = await conn.fetchrow("""
                    SELECT * FROM campaign_joins WHERE campaign_id = $1 AND user_id = $2 AND status = 'pending' FOR UPDATE
                """, cid, uid)
                if not campaign or not join:
                    await message.answer("Active campaign or pending join not found.")
                    return
                if campaign["verified_joins"] >= campaign["target_members"]:
                    await message.answer("Campaign target already reached.")
                    return
                reward_usdt = campaign["reward_per_join"]
                if campaign["spent"] + reward_usdt > campaign["total_budget"]:
                    await message.answer("Campaign budget exhausted.")
                    return
                # Reward points are a configurable conversion. Default: 1000 points per 1 USDT.
                points_per_usdt = Decimal(str(await get_setting("points_per_usdt", "1000")))
                points = int((reward_usdt * points_per_usdt).to_integral_value())
                await conn.execute("UPDATE campaign_joins SET status = 'approved' WHERE join_id = $1", join["join_id"])
                await conn.execute("""
                    UPDATE campaigns SET verified_joins = verified_joins + 1,
                        spent = spent + $1, updated_at = NOW()
                    WHERE campaign_id = $2
                """, reward_usdt, cid)
                await conn.execute("""
                    UPDATE advertiser_wallets SET reserved = GREATEST(reserved - $1, 0),
                        updated_at = NOW() WHERE user_id = $2
                """, reward_usdt, campaign["advertiser_id"])
                await conn.execute("UPDATE users SET points = points + $1 WHERE user_id = $2", points, uid)
                if campaign["verified_joins"] + 1 >= campaign["target_members"]:
                    remaining = campaign["total_budget"] - (campaign["spent"] + reward_usdt)
                    await conn.execute("""
                        UPDATE campaigns SET status = 'completed' WHERE campaign_id = $1
                    """, cid)
                    if remaining > 0:
                        await conn.execute("""
                            UPDATE advertiser_wallets SET available = available + $1,
                                reserved = GREATEST(reserved - $1, 0), updated_at = NOW()
                            WHERE user_id = $2
                        """, remaining, campaign["advertiser_id"])
        await message.answer(f"✅ Join verified. User {uid} received {points} points. Campaign #{cid}.")
        try:
            await bot.send_message(uid, f"🎉 Your campaign join was verified! +{points} points.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /approvejoin CAMPAIGN_ID USER_ID")


@dp.message(Command("rejectjoin"))
async def reject_join(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        _, cid_text, uid_text = message.text.split()
        cid, uid = int(cid_text), int(uid_text)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE campaign_joins SET status = 'rejected'
                WHERE campaign_id = $1 AND user_id = $2 AND status = 'pending'
                RETURNING user_id
            """, cid, uid)
        if not row:
            await message.answer("Pending join not found.")
            return
        await message.answer("❌ Join rejected; no reward was paid.")
        try:
            await bot.send_message(uid, f"Your join proof for campaign #{cid} was rejected.")
        except Exception:
            pass
    except (ValueError, IndexError):
        await message.answer("Use: /rejectjoin CAMPAIGN_ID USER_ID")


@dp.message(Command("setpointsrate"))
async def set_points_rate(message: Message):
    if not await is_admin(message.from_user.id):
        return
    try:
        rate = Decimal(message.text.split()[1])
        if rate <= 0:
            raise ValueError
        await set_setting("points_per_usdt", str(rate))
        await message.answer(f"✅ Campaign reward conversion set: 1 USDT = {rate} points.")
    except (ValueError, IndexError, InvalidOperation):
        await message.answer("Use: /setpointsrate POINTS_PER_USDT")


# ---------------- USER MENU ----------------

@dp.message(lambda m: m.text == "🎬 Watch Ads")
async def watch_ads(message: Message):
    if not await allowed(message):
        return
    uid = message.from_user.id
    reward = await get_setting("ad_reward", REWARD_POINTS)
    async with db_pool.acquire() as conn:
        result = await conn.fetchrow("""
            UPDATE users SET points = points + $2, last_ad_at = NOW()
            WHERE user_id = $1 AND (
                last_ad_at IS NULL OR last_ad_at <= NOW() - INTERVAL '30 seconds'
            ) RETURNING points
        """, uid, reward)
        if result:
            await message.answer(
                f"🎁 TEST MODE: +{reward} points added.\nBalance: {result['points']} points\n\n"
                "Real rewarded ads are not connected yet. Do not treat this as ad revenue."
            )
            return
        last = await conn.fetchval("SELECT last_ad_at FROM users WHERE user_id = $1", uid)
    remaining = max(1, math.ceil((last + timedelta(seconds=COOLDOWN_SECONDS) - datetime.now(timezone.utc)).total_seconds()))
    await message.answer(f"⏳ Please wait {remaining}s before trying again.")


@dp.message(lambda m: m.text == "💰 Balance")
async def balance(message: Message):
    if not await allowed(message):
        return
    user = await get_user(message.from_user.id)
    await message.answer(f"💰 Your current balance: {user['points']} points")


@dp.message(lambda m: m.text == "📋 Tasks")
async def tasks(message: Message):
    if not await allowed(message):
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT task_id, title, reward FROM tasks WHERE active = TRUE ORDER BY task_id DESC")
    if not rows:
        await message.answer("No tasks available right now.")
        return
    out = "📋 AVAILABLE TASKS\n\n"
    for r in rows:
        out += f"ID: {r['task_id']}\n{r['title']}\nReward: {r['reward']} points\n\n"
    out += "Submit proof using:\n/submit TASK_ID YOUR_PROOF\n\n"
    async with db_pool.acquire() as conn:
        campaigns = await conn.fetch("""
            SELECT campaign_id, title, channel_link, target_members, verified_joins, reward_per_join
            FROM campaigns WHERE status = 'active' AND verified_joins < target_members
            ORDER BY campaign_id DESC LIMIT 15
        """)
    if campaigns:
        out += "📢 PAID PROMOTION TASKS\n\n"
        for c in campaigns:
            out += (f"Campaign #{c['campaign_id']}: {c['title']}\n"
                    f"Progress: {c['verified_joins']}/{c['target_members']}\n"
                    f"Reward: {c['reward_per_join']} USDT equivalent\n"
                    f"Join: /joincampaign {c['campaign_id']}\n\n")
    await message.answer(out)


@dp.message(lambda m: m.text == "💳 Withdraw")
async def withdraw_menu(message: Message):
    if not await allowed(message):
        return
    minimum = await get_setting("min_withdraw", MIN_WITHDRAWAL)
    await message.answer(f"💳 Minimum withdrawal: {minimum} points\n\n/withdraw AMOUNT YOUR_CRYPTO_WALLET")


@dp.message()
async def fallback(message: Message):
    await message.answer("Please use the menu buttons below. 👇", reply_markup=keyboard)


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
