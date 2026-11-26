import asyncio
import os
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ইউজারের পয়েন্ট রাখার সহজ ডিকশনারি (পরীক্ষার জন্য)
user_points: dict[int, int] = {}

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎬 Watch Ads")],
        [KeyboardButton(text="📋 Tasks"), KeyboardButton(text="💰 Balance")],
        [KeyboardButton(text="💳 Withdraw")]
    ],
    resize_keyboard=True
)


@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    user_points.setdefault(user_id, 0)
    await message.answer(
        "Welcome to EarnWave! 🌟\n\n"
        "Earn points by watching ads and completing tasks.",
        reply_markup=keyboard
    )


@dp.message(lambda msg: msg.text == "🎬 Watch Ads")
async def watch_ads(message: Message):
    user_id = message.from_user.id
    current = user_points.get(user_id, 0) + 10
    user_points[user_id] = current
    await message.answer(
        f"🎉 You earned +10 points!\n"
        f"💰 Your balance: {current} points"
    )


@dp.message(lambda msg: msg.text == "💰 Balance")
async def balance(message: Message):
    user_id = message.from_user.id
    current = user_points.get(user_id, 0)
    await message.answer(f"💰 Your current balance: {current} points")


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
    current = user_points.get(user_id, 0)
    await message.answer(
        f"💳 Withdraw Info:\n"
        f"• Current balance: {current} points\n"
        f"• Minimum required: 100 points"
    )


@dp.message()
async def fallback(message: Message):
    await message.answer("Please use the menu buttons below. 👇", reply_markup=keyboard)


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


async def main():
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
