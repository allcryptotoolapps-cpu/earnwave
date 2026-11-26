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

# Admin Telegram User ID
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


async def setup_database():
    global db_pool

    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                points BIGINT NOT NULL DEFAULT 0,
                last_ad_at TIMESTAMPTZ
            )
        """)

    print("PostgreSQL connected successfully!")


async def register_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, points)
            VALUES ($1, 0)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)


async def get_user(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT points, last_ad_at
            FROM users
            WHERE user_id = $1
        """, user_id)


@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    await register_user(user_id)

    await message.answer(
        "Welcome to EarnWave! 🌟\n\n"
        "Earn points by watching ads and completing tasks.",
        reply_markup=keyboard
    )


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    user_id = message.from_user.id

    if user_id not in ADMIN_IDS:
        await message.answer("⛔ You are not authorized to access the Admin Panel.")
        return

    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval(
            "SELECT COUNT(*) FROM users"
        )
        total_points = await conn.fetchval(
            "SELECT COALESCE(SUM(points), 0) FROM users"
        )

    await message.answer(
        "🛠 EARNWAVE ADMIN PANEL\n\n"
        f"👥 Total Users: {total_users}\n"
        f"💰 Total Points: {total_points}\n\n"
        "Admin access confirmed!\n"
        "More management features will be added next."
    )

@dp.message(Command("users"))
async def admin_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT user_id, points FROM users ORDER BY points DESC LIMIT 20"
        )

    if not users:
        await message.answer("No users found.")
        return

    text = "👥 USER LIST\n\n"
    for user in users:
        text += f"ID: {user['user_id']} | Points: {user['points']}\n"

    await message.answer(text)


@dp.message(Command("addpoints"))
async def add_points(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    try:
        _, user_id, amount = message.text.split()
        user_id = int(user_id)
        amount = int(amount)

        if amount <= 0:
            raise ValueError

        async with db_pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET points = points + $1 WHERE user_id = $2",
                amount, user_id
            )

        if result == "UPDATE 0":
            await message.answer("User not found.")
        else:
            await message.answer(f"✅ Added {amount} points to {user_id}.")

    except:
        await message.answer("Use: /addpoints USER_ID AMOUNT")


@dp.message(Command("removepoints"))
async def remove_points(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    try:
        _, user_id, amount = message.text.split()
        user_id = int(user_id)
        amount = int(amount)

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
            await message.answer(f"✅ Removed {amount} points from {user_id}.")

    except:
        await message.answer("Use: /removepoints USER_ID AMOUNT")
        

@dp.message(lambda msg: msg.text == "🎬 Watch Ads")
async def watch_ads(message: Message):
    user_id = message.from_user.id
    await register_user(user_id)

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
        """, user_id, REWARD_POINTS)

        if result:
            await message.answer(
                f"🎉 You earned +{REWARD_POINTS} points!\n"
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
        f"⏳ Please wait {remaining}s before watching another ad."
    )


@dp.message(lambda msg: msg.text == "💰 Balance")
async def balance(message: Message):
    user_id = message.from_user.id
    await register_user(user_id)
    user = await get_user(user_id)

    await message.answer(
        f"💰 Your current balance: {user['points']} points"
    )


@dp.message(lambda msg: msg.text == "📋 Tasks")
async def tasks(message: Message):
    await message.answer(
        "📋 Available Tasks:\n"
        "• Watch ads regularly to collect points.\n"
        "• More tasks will be added soon!"
    )


@dp.message(lambda msg: msg.text == "💳 Withdraw")
async def withdraw(message: Message):
    user_id = message.from_user.id
    await register_user(user_id)
    user = await get_user(user_id)
    current = user["points"]

    if current < 100:
        await message.answer(
            f"💳 Withdraw Info:\n"
            f"• Current balance: {current} points\n"
            f"• Minimum required: 100 points"
        )
    else:
        await message.answer(
            f"✅ You are eligible to withdraw!\n"
            f"💰 Current balance: {current} points\n"
            "Please contact admin to process your request."
        )


@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Please use the menu buttons below. 👇",
        reply_markup=keyboard
    )


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
