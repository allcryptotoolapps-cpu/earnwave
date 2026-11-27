import asyncio
import os
import math
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import aiohttp
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
MIN_WITHDRAWAL = 5000                 # 1,000 points = 1 USDT; minimum = 5 USDT
REFERRAL_BONUS = 10
POINTS_PER_USDT = Decimal("1000")
ADMIN_IDS = {8435970103}

USDT_TRC20_ADDRESS = os.getenv("USDT_TRC20_ADDRESS", "")
USDT_BEP20_ADDRESS = os.getenv("USDT_BEP20_ADDRESS", "")
TRON_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
BSC_USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

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


def parse_usdt(value):
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid USDT amount")
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -8:
        raise ValueError("USDT amount must be positive with at most 8 decimals")
    return amount.quantize(Decimal("0.00000001"))


def valid_txid(txid):
    value = txid.strip()
    if value.lower().startswith("0x"):
        value = value[2:]
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))


def valid_wallet(network, wallet):
    wallet = wallet.strip()
    if network == "BEP20":
        return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", wallet))
    if network == "TRC20":
        # TRON mainnet Base58Check addresses start with T and are 34 chars.
        return bool(re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", wallet))
    return False


async def setup_database():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS users(
          user_id BIGINT PRIMARY KEY, points BIGINT NOT NULL DEFAULT 0,
          last_ad_at TIMESTAMPTZ, is_banned BOOLEAN NOT NULL DEFAULT FALSE,
          referred_by BIGINT, referral_paid BOOLEAN NOT NULL DEFAULT FALSE,
          language TEXT NOT NULL DEFAULT 'en');
        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_paid BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en';

        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(
          task_id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL,
          reward BIGINT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE);
        CREATE TABLE IF NOT EXISTS submissions(
          submission_id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
          task_id BIGINT NOT NULL, proof TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS withdrawals(
          withdrawal_id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
          amount BIGINT NOT NULL, wallet TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ DEFAULT NOW());
        ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS txid TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS unique_user_payout_txid
          ON withdrawals(LOWER(txid)) WHERE txid IS NOT NULL;

        CREATE TABLE IF NOT EXISTS advertiser_wallets(
          user_id BIGINT PRIMARY KEY, available NUMERIC(20,8) NOT NULL DEFAULT 0,
          reserved NUMERIC(20,8) NOT NULL DEFAULT 0,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS advertiser_transactions(
          tx_id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
          tx_type TEXT NOT NULL, amount NUMERIC(20,8) NOT NULL,
          network TEXT, reference TEXT, status TEXT NOT NULL DEFAULT 'pending',
          note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE UNIQUE INDEX IF NOT EXISTS unique_advertiser_deposit_txid
          ON advertiser_transactions(LOWER(reference))
          WHERE tx_type='deposit' AND reference IS NOT NULL;

        CREATE TABLE IF NOT EXISTS campaigns(
          campaign_id BIGSERIAL PRIMARY KEY, advertiser_id BIGINT NOT NULL,
          title TEXT NOT NULL, channel_link TEXT NOT NULL,
          target_members INTEGER NOT NULL, reward_per_join NUMERIC(20,8) NOT NULL,
          total_budget NUMERIC(20,8) NOT NULL, spent NUMERIC(20,8) NOT NULL DEFAULT 0,
          verified_joins INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending_payment',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), chat_id TEXT);
        ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS chat_id TEXT;
        ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS budget_reserved BOOLEAN NOT NULL DEFAULT TRUE;

        CREATE TABLE IF NOT EXISTS campaign_joins(
          join_id BIGSERIAL PRIMARY KEY,
          campaign_id BIGINT NOT NULL REFERENCES campaigns(campaign_id),
          user_id BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(campaign_id,user_id));
        CREATE TABLE IF NOT EXISTS advertiser_withdrawals(
          withdrawal_id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
          amount NUMERIC(20,8) NOT NULL, network TEXT NOT NULL,
          wallet TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        ALTER TABLE advertiser_withdrawals ADD COLUMN IF NOT EXISTS txid TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS unique_adv_payout_txid
          ON advertiser_withdrawals(LOWER(txid)) WHERE txid IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS unique_open_task_submission
          ON submissions(user_id,task_id) WHERE status IN ('pending','approved');
        """)
        await c.execute("""
          INSERT INTO settings(key,value) VALUES
          ('min_withdraw','5000'),('points_per_usdt','1000'),
          ('referral_bonus','10'),('adv_min_withdraw','10')
          ON CONFLICT(key) DO NOTHING
        """)
        # Keep the minimum at 5 USDT (1,000 points = 1 USDT).
        await c.execute("""
          INSERT INTO settings(key,value) VALUES('min_withdraw','5000')
          ON CONFLICT(key) DO UPDATE SET value =
            GREATEST(COALESCE(NULLIF(settings.value,'')::BIGINT,5000),5000)::TEXT
        """)
        # Existing test campaigns created by the previous version must never
        # become real paid campaigns after this migration.
        await c.execute("""
          UPDATE campaigns
          SET is_test=TRUE, budget_reserved=FALSE
          WHERE title LIKE 'TEST:%'
        """)
    print("PostgreSQL connected")


async def get_setting(key, default):
    async with db_pool.acquire() as c:
        value = await c.fetchval("SELECT value FROM settings WHERE key=$1", key)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return value


async def set_setting(key, value):
    async with db_pool.acquire() as c:
        await c.execute("""
          INSERT INTO settings(key,value) VALUES($1,$2)
          ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
        """, key, str(value))


async def register_user(uid, ref=None):
    async with db_pool.acquire() as c:
        async with c.transaction():
            inserted = await c.fetchval("""
              INSERT INTO users(user_id,referred_by) VALUES($1,$2)
              ON CONFLICT(user_id) DO NOTHING RETURNING user_id
            """, uid, ref if ref != uid else None)
            if inserted:
                await c.execute(
                    "INSERT INTO advertiser_wallets(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
                if ref and ref != uid:
                    bonus = int(await c.fetchval(
                        "SELECT value FROM settings WHERE key='referral_bonus'") or REFERRAL_BONUS)
                    valid = await c.fetchval("SELECT user_id FROM users WHERE user_id=$1", ref)
                    if valid:
                        await c.execute("UPDATE users SET points=points+$1 WHERE user_id=$2", bonus, ref)
                        await c.execute("UPDATE users SET referral_paid=TRUE WHERE user_id=$1", uid)


async def get_user(uid):
    async with db_pool.acquire() as c:
        return await c.fetchrow(
            "SELECT points,last_ad_at,is_banned,language FROM users WHERE user_id=$1", uid)


async def is_admin(uid):
    return uid in ADMIN_IDS


async def allowed(message):
    if await is_admin(message.from_user.id):
        return True
    user = await get_user(message.from_user.id)
    if not user:
        await register_user(message.from_user.id)
        user = await get_user(message.from_user.id)
    if user and user["is_banned"]:
        await message.answer("⛔ Your account is banned.")
        return False
    return True


async def ensure_wallet(uid):
    async with db_pool.acquire() as c:
        await c.execute("INSERT INTO advertiser_wallets(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
        return await c.fetchrow(
            "SELECT available,reserved FROM advertiser_wallets WHERE user_id=$1", uid)


async def verify_deposit_on_blockchain(network, txid, amount):
    txid = txid.strip()
    raw = txid[2:] if txid.lower().startswith("0x") else txid
    if not valid_txid(txid):
        return False
    if network == "TRC20":
        address = await get_setting("deposit_address_TRC20", USDT_TRC20_ADDRESS)
        api_key = os.getenv("TRONGRID_API_KEY", "")
        if not address or not api_key:
            raise RuntimeError("TRON deposit address or TRONGRID_API_KEY is not configured")
        expected = int(amount * Decimal(10**6))
        url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
        params = {"only_confirmed":"true","only_to":"true","limit":200,
                  "contract_address":TRON_USDT_CONTRACT,"order_by":"block_timestamp,desc"}
        headers = {"TRON-PRO-API-KEY":api_key}
        async with aiohttp.ClientSession() as s:
            for _ in range(20):
                async with s.get(url,params=params,headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=25)) as r:
                    r.raise_for_status()
                    data = await r.json()
                for item in data.get("data",[]):
                    if (item.get("transaction_id","").lower()==raw.lower()
                        and item.get("to")==address
                        and item.get("token_info",{}).get("address")==TRON_USDT_CONTRACT
                        and int(item.get("value","0"))==expected):
                        return True
                fp = data.get("meta",{}).get("fingerprint")
                if not fp: break
                params["fingerprint"] = fp
        return False
    if network == "BEP20":
        address = await get_setting("deposit_address_BEP20", USDT_BEP20_ADDRESS)
        if not valid_wallet("BEP20", address or ""):
            raise RuntimeError("BEP20 deposit address is not configured correctly")
        expected = int(amount * Decimal(10**18))
        async with aiohttp.ClientSession() as s:
            async def rpc(method, params):
                async with s.post("https://bsc-dataseed.binance.org/",
                    json={"jsonrpc":"2.0","id":1,"method":method,"params":params},
                    timeout=aiohttp.ClientTimeout(total=25)) as r:
                    r.raise_for_status()
                    data = await r.json()
                if data.get("error") or "result" not in data:
                    raise RuntimeError("BSC RPC verification failed")
                return data["result"]
            if await rpc("eth_chainId",[]) != "0x38": return False
            receipt = await rpc("eth_getTransactionReceipt",["0x"+raw])
            if not receipt or receipt.get("status")!="0x1": return False
            latest = await rpc("eth_blockNumber",[])
            if int(latest,16)-int(receipt["blockNumber"],16)+1 < 12: return False
            for log in receipt.get("logs",[]):
                topics=log.get("topics",[])
                if (log.get("address","").lower()==BSC_USDT_CONTRACT.lower()
                    and len(topics)>=3 and topics[0].lower()==TRANSFER_TOPIC
                    and topics[2][-40:].lower()==address[2:].lower()
                    and int(log.get("data","0x0"),16)==expected):
                    return True
        return False
    return False


# START / LANGUAGE
@dp.message(CommandStart())
async def start(message: Message):
    args=(message.text or "").split()
    ref=None
    if len(args)>1 and args[1].startswith("ref_"):
        try: ref=int(args[1][4:])
        except ValueError: pass
    await register_user(message.from_user.id,ref)
    if await allowed(message):
        await message.answer(
            "Welcome to EarnWave! 🌍\nEarn points through available tasks and verified campaigns.\n"
            "Use 🌐 Language to choose your language.", reply_markup=keyboard)


@dp.message(lambda m:m.text=="🌐 Language")
async def language_menu(message: Message):
    if await allowed(message):
        await message.answer("Choose: /lang en | /lang bn | /lang hi | /lang ur | /lang ar | /lang es | /lang fr")


@dp.message(Command("lang"))
async def set_language(message: Message):
    if not await allowed(message): return
    p=(message.text or "").split()
    if len(p)!=2 or p[1] not in {"en","bn","hi","ur","ar","es","fr"}:
        await message.answer("Use: /lang en|bn|hi|ur|ar|es|fr"); return
    async with db_pool.acquire() as c:
        await c.execute("UPDATE users SET language=$1 WHERE user_id=$2",p[1],message.from_user.id)
    await message.answer("✅ Language preference saved. Some messages remain in English.")


# ADMIN
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not await is_admin(message.from_user.id): return
    async with db_pool.acquire() as c:
        users=await c.fetchval("SELECT COUNT(*) FROM users")
        points=await c.fetchval("SELECT COALESCE(SUM(points),0) FROM users")
        camps=await c.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='pending_payment'")
        deposits=await c.fetchval("SELECT COUNT(*) FROM advertiser_transactions WHERE tx_type='deposit' AND status='pending'")
        funds=await c.fetchval("SELECT COALESCE(SUM(available),0) FROM advertiser_wallets")
    await message.answer(f"🛠 EARNWAVE ADMIN\nUsers: {users}\nUser points: {points}\nMinimum withdrawal: {await get_setting('min_withdraw',MIN_WITHDRAWAL)}\nPending campaigns: {camps}\nPending deposits: {deposits}\nAdvertiser available: {funds} USDT\n/helpadmin")


@dp.message(Command("helpadmin"))
async def helpadmin(message: Message):
    if not await is_admin(message.from_user.id): return
    await message.answer(
      "ADMIN:\n/users /addpoints ID AMOUNT /removepoints ID AMOUNT /ban ID /unban ID\n"
      "/addtask REWARD | TITLE /tasklist /deltask ID /submissions /approve ID /reject ID\n"
      "/withdrawals [pending|approved|paid|rejected] /approvewithdraw ID /rejectwithdraw ID /markpaid ID TXID\n"
      "/broadcast TEXT /setminwithdraw POINTS /setrefbonus POINTS\n"
      "ADVERTISER:\n/deposits /approvedeposit TX_ID /rejectdeposit TX_ID\n"
      "/campaigns /approvecampaign ID /rejectcampaign ID\n"
      "/advertiserwithdrawals /approveadvwithdraw ID /markadvpaid ID TXID /rejectadvwithdraw ID\n"
      "/setdepositaddress NETWORK ADDRESS /setadvminwithdraw AMOUNT /setpointsrate RATE")


@dp.message(Command("users"))
async def admin_users(message: Message):
    if not await is_admin(message.from_user.id): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT user_id,points,is_banned FROM users ORDER BY points DESC LIMIT 20")
    await message.answer("👥 USERS\n"+"\n".join(
        f"{r['user_id']} | {r['points']} | {'BANNED' if r['is_banned'] else 'ACTIVE'}" for r in rows) if rows else "No users.")


@dp.message(Command("addpoints"))
async def add_points(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        _,u,a=message.text.split(); u=int(u); a=int(a)
        if a<=0: raise ValueError
        async with db_pool.acquire() as c: res=await c.execute("UPDATE users SET points=points+$1 WHERE user_id=$2",a,u)
        await message.answer("User not found." if res=="UPDATE 0" else f"Added {a} points.")
    except (ValueError,AttributeError): await message.answer("Use: /addpoints ID AMOUNT")


@dp.message(Command("removepoints"))
async def remove_points(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        _,u,a=message.text.split(); u=int(u); a=int(a)
        if a<=0: raise ValueError
        async with db_pool.acquire() as c: res=await c.execute("UPDATE users SET points=GREATEST(points-$1,0) WHERE user_id=$2",a,u)
        await message.answer("User not found." if res=="UPDATE 0" else f"Removed up to {a} points.")
    except (ValueError,AttributeError): await message.answer("Use: /removepoints ID AMOUNT")


@dp.message(Command("ban"))
async def ban_user(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        uid=int(message.text.split()[1])
        async with db_pool.acquire() as c: res=await c.execute("UPDATE users SET is_banned=TRUE WHERE user_id=$1",uid)
        await message.answer("User not found." if res=="UPDATE 0" else f"User {uid} banned.")
    except (ValueError,IndexError): await message.answer("Use: /ban ID")


@dp.message(Command("unban"))
async def unban_user(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        uid=int(message.text.split()[1])
        async with db_pool.acquire() as c: res=await c.execute("UPDATE users SET is_banned=FALSE WHERE user_id=$1",uid)
        await message.answer("User not found." if res=="UPDATE 0" else f"User {uid} unbanned.")
    except (ValueError,IndexError): await message.answer("Use: /unban ID")


# TASKS
@dp.message(Command("addtask"))
async def add_task(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        raw=message.text.split(maxsplit=1)[1]; reward,title=raw.split("|",1)
        reward=int(reward.strip()); title=title.strip()
        if reward<=0 or not title: raise ValueError
        async with db_pool.acquire() as c: tid=await c.fetchval("INSERT INTO tasks(title,reward) VALUES($1,$2) RETURNING task_id",title,reward)
        await message.answer(f"Task #{tid} added.")
    except (ValueError,IndexError): await message.answer("Use: /addtask 50 | Task title")


@dp.message(Command("tasklist"))
async def task_list(message: Message):
    if not await is_admin(message.from_user.id): return
    async with db_pool.acquire() as c: rows=await c.fetch("SELECT task_id,title,reward,active FROM tasks ORDER BY task_id DESC")
    await message.answer("\n\n".join(f"#{r['task_id']} {r['title']} | {r['reward']} points | {'Active' if r['active'] else 'Deleted'}" for r in rows) or "No tasks.")


@dp.message(Command("deltask"))
async def delete_task(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        tid=int(message.text.split()[1])
        async with db_pool.acquire() as c: res=await c.execute("UPDATE tasks SET active=FALSE WHERE task_id=$1",tid)
        await message.answer("Task not found." if res=="UPDATE 0" else "Task deactivated.")
    except (ValueError,IndexError): await message.answer("Use: /deltask ID")


@dp.message(Command("submit"))
async def submit_task(message: Message):
    if not await allowed(message): return
    try:
        _,tid,proof=message.text.split(maxsplit=2); tid=int(tid)
        async with db_pool.acquire() as c:
            exists=await c.fetchval("SELECT 1 FROM tasks WHERE task_id=$1 AND active=TRUE",tid)
            if not exists: await message.answer("Task not found."); return
            sid=await c.fetchval("""INSERT INTO submissions(user_id,task_id,proof) VALUES($1,$2,$3)
              ON CONFLICT(user_id,task_id) WHERE status IN ('pending','approved') DO NOTHING RETURNING submission_id""",
              message.from_user.id,tid,proof)
        await message.answer(f"Submission received: #{sid}" if sid else "You already submitted this task.")
    except (ValueError,IndexError): await message.answer("Use: /submit TASK_ID PROOF")


@dp.message(Command("submissions"))
async def submissions(message: Message):
    if not await is_admin(message.from_user.id): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT submission_id,user_id,task_id,proof FROM submissions WHERE status='pending' ORDER BY submission_id DESC LIMIT 20")
    await message.answer("\n\n".join(f"#{r['submission_id']} user {r['user_id']} task {r['task_id']}\n{r['proof'][:200]}" for r in rows) or "No pending submissions.")


@dp.message(Command("approve"))
async def approve_submission(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        sid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            async with c.transaction():
                r=await c.fetchrow("""SELECT s.user_id,t.reward FROM submissions s JOIN tasks t USING(task_id)
                  WHERE s.submission_id=$1 AND s.status='pending' FOR UPDATE OF s""",sid)
                if r:
                    await c.execute("UPDATE submissions SET status='approved' WHERE submission_id=$1",sid)
                    await c.execute("UPDATE users SET points=points+$1 WHERE user_id=$2",r["reward"],r["user_id"])
        if not r: await message.answer("Pending submission not found."); return
        await message.answer("Approved and points credited.")
        try: await bot.send_message(r["user_id"],f"Task approved: +{r['reward']} points.")
        except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /approve ID")


@dp.message(Command("reject"))
async def reject_submission(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        sid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            r=await c.fetchrow("UPDATE submissions SET status='rejected' WHERE submission_id=$1 AND status='pending' RETURNING user_id",sid)
        await message.answer("Rejected." if r else "Pending submission not found.")
        if r:
            try: await bot.send_message(r["user_id"],"Your task submission was rejected.")
            except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /reject ID")


# USER WITHDRAWALS
@dp.message(Command("withdraw"))
async def create_withdrawal(message: Message):
    if not await allowed(message): return
    try:
        _,amount_text,network,wallet=message.text.split(maxsplit=3)
        amount=int(amount_text); network=network.upper()
        if amount<=0 or network not in {"TRC20","BEP20"} or not valid_wallet(network,wallet):
            raise ValueError
        minimum=int(await get_setting("min_withdraw",MIN_WITHDRAWAL))
        if minimum < 5000:
            minimum=5000
        if amount<minimum:
            await message.answer(f"Minimum withdrawal is {minimum} points (${minimum/1000:.2f} USDT)."); return
        uid=message.from_user.id
        async with db_pool.acquire() as c:
            async with c.transaction():
                user=await c.fetchrow("SELECT points FROM users WHERE user_id=$1 FOR UPDATE",uid)
                if not user or user["points"]<amount:
                    await message.answer("Insufficient points."); return
                await c.execute("UPDATE users SET points=points-$1 WHERE user_id=$2",amount,uid)
                wid=await c.fetchval("INSERT INTO withdrawals(user_id,amount,wallet) VALUES($1,$2,$3) RETURNING withdrawal_id",uid,amount,f"{network}|{wallet}")
        await message.answer(f"Withdrawal request #{wid} submitted.\n{amount} points = ${amount/1000:.2f} USDT.\nAdmin processes payment manually.")
    except (ValueError,IndexError): await message.answer("Use: /withdraw AMOUNT TRC20|BEP20 WALLET_ADDRESS")


@dp.message(Command("withdrawals"))
async def pending_withdrawals(message: Message):
    if not await is_admin(message.from_user.id): return
    parts=message.text.split(maxsplit=1); status=parts[1].lower() if len(parts)>1 else "all"
    if status not in {"all","pending","approved","paid","rejected"}:
        await message.answer("Use: /withdrawals [pending|approved|paid|rejected]"); return
    async with db_pool.acquire() as c:
        if status=="all":
            rows=await c.fetch("SELECT * FROM withdrawals ORDER BY withdrawal_id DESC LIMIT 30")
        else: rows=await c.fetch("SELECT * FROM withdrawals WHERE status=$1 ORDER BY withdrawal_id DESC LIMIT 30",status)
    out=[]
    for r in rows:
        net,_,*rest=r["wallet"].partition("|")
        wallet=rest[0] if rest else net
        network=net if rest else "legacy"
        out.append(f"#{r['withdrawal_id']} user {r['user_id']} | {r['amount']} pts (${r['amount']/1000:.2f})\n{network} {wallet}\n{r['status']} | TXID: {r['txid'] or '-'}")
    await message.answer("\n\n".join(out) or "No withdrawals.")


@dp.message(Command("approvewithdraw"))
async def approve_withdrawal(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        wid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            r=await c.fetchrow("UPDATE withdrawals SET status='approved' WHERE withdrawal_id=$1 AND status='pending' RETURNING user_id,amount",wid)
        if not r: await message.answer("Pending request not found."); return
        await message.answer(f"Approved. Send ${r['amount']/1000:.2f} manually, then /markpaid {wid} TXID")
        try: await bot.send_message(r["user_id"],f"Withdrawal #{wid} approved; payment is being processed.")
        except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /approvewithdraw ID")


@dp.message(Command("markpaid"))
async def mark_paid(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        _,wid,txid=message.text.split(maxsplit=2); wid=int(wid)
        if not valid_txid(txid): raise ValueError
        txid=txid.lower().removeprefix("0x")
        async with db_pool.acquire() as c:
            r=await c.fetchrow("UPDATE withdrawals SET status='paid',txid=$2 WHERE withdrawal_id=$1 AND status='approved' RETURNING user_id,amount",wid,txid)
        if not r: await message.answer("Approved withdrawal not found, already paid, or TXID already used."); return
        await message.answer(f"Withdrawal #{wid} marked paid. TXID: {txid}")
        try: await bot.send_message(r["user_id"],f"Withdrawal paid: ${r['amount']/1000:.2f} USDT\nTXID: {txid}")
        except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /markpaid ID 64_CHARACTER_TXID")


@dp.message(Command("mywithdrawals"))
async def my_withdrawals(message: Message):
    if not await allowed(message): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT withdrawal_id,amount,wallet,status,txid FROM withdrawals WHERE user_id=$1 ORDER BY withdrawal_id DESC LIMIT 10",message.from_user.id)
    await message.answer("\n\n".join(f"#{r['withdrawal_id']} ${r['amount']/1000:.2f} | {r['status']}\nTXID: {r['txid'] or '-'}" for r in rows) or "No withdrawal history.")


@dp.message(Command("rejectwithdraw"))
async def reject_withdrawal(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        wid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            async with c.transaction():
                r=await c.fetchrow("UPDATE withdrawals SET status='rejected' WHERE withdrawal_id=$1 AND status='pending' RETURNING user_id,amount",wid)
                if r: await c.execute("UPDATE users SET points=points+$1 WHERE user_id=$2",r["amount"],r["user_id"])
        await message.answer("Rejected and points refunded." if r else "Pending request not found.")
    except (ValueError,IndexError): await message.answer("Use: /rejectwithdraw ID")


# REFERRALS / SETTINGS / BROADCAST
@dp.message(Command("referral"))
async def referral(message: Message):
    if not await allowed(message): return
    me=await bot.get_me()
    async with db_pool.acquire() as c: count=await c.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1",message.from_user.id)
    bonus=await get_setting("referral_bonus",REFERRAL_BONUS)
    await message.answer(f"Referrals: {count}\nBonus: {bonus} points\nhttps://t.me/{me.username}?start=ref_{message.from_user.id}")


@dp.message(Command("setminwithdraw"))
async def set_min_withdraw(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        amount=int(message.text.split()[1])
        if amount<5000: raise ValueError
        await set_setting("min_withdraw",amount)
        await message.answer(f"Minimum withdrawal set to {amount} points.")
    except (ValueError,IndexError): await message.answer("Use: /setminwithdraw AMOUNT (minimum 5000)")


@dp.message(Command("setrefbonus"))
async def set_ref_bonus(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        n=int(message.text.split()[1])
        if n<0: raise ValueError
        await set_setting("referral_bonus",n); await message.answer("Referral bonus updated.")
    except (ValueError,IndexError): await message.answer("Use: /setrefbonus AMOUNT")


@dp.message(Command("broadcast"))
async def broadcast(message: Message):
    if not await is_admin(message.from_user.id): return
    body=message.text.partition(" ")[2].strip()
    if not body: await message.answer("Use: /broadcast MESSAGE"); return
    async with db_pool.acquire() as c: users=await c.fetch("SELECT user_id FROM users WHERE is_banned=FALSE")
    sent=failed=0
    for u in users:
        try: await bot.send_message(u["user_id"],body); sent+=1
        except Exception: failed+=1
        await asyncio.sleep(0.04)
    await message.answer(f"Broadcast done. Sent: {sent}; failed: {failed}")


# ADVERTISER WALLET / DEPOSITS
@dp.message(lambda m:m.text in ("👛 Wallet","/wallet"))
async def wallet_menu(message: Message):
    if not await allowed(message): return
    w=await ensure_wallet(message.from_user.id)
    await message.answer(f"👛 ADVERTISER WALLET\nAvailable: {w['available']} USDT\nReserved: {w['reserved']} USDT\n/deposit\n/advwithdraw AMOUNT NETWORK WALLET\n/transactions")


@dp.message(Command("deposit"))
async def deposit(message: Message):
    if not await allowed(message): return
    await ensure_wallet(message.from_user.id)
    a=await get_setting("deposit_address_TRC20",USDT_TRC20_ADDRESS)
    b=await get_setting("deposit_address_BEP20",USDT_BEP20_ADDRESS)
    await message.answer(f"USDT DEPOSIT\nTRC20: {a or 'Not configured'}\nBEP20: {b or 'Not configured'}\nSend on the matching network, then:\n/depositproof AMOUNT NETWORK TXID\nCredit occurs only after blockchain verification.")


@dp.message(Command("depositproof"))
async def deposit_proof(message: Message):
    if not await allowed(message): return
    try:
        _,amt,network,txid=message.text.split(maxsplit=3)
        amount=parse_usdt(amt); network=network.upper()
        if network not in {"TRC20","BEP20"} or not valid_txid(txid): raise ValueError
        txid=txid.lower().removeprefix("0x")
        async with db_pool.acquire() as c:
            tx=await c.fetchval("""INSERT INTO advertiser_transactions(user_id,tx_type,amount,network,reference,note)
              VALUES($1,'deposit',$2,$3,$4,'Deposit proof') ON CONFLICT DO NOTHING RETURNING tx_id""",
              message.from_user.id,amount,network,txid)
        if not tx: await message.answer("This TXID has already been submitted."); return
        await message.answer(f"Deposit proof #{tx} submitted. Admin will verify it on-chain.")
        for aid in ADMIN_IDS:
            try: await bot.send_message(aid,f"Deposit #{tx}: user {message.from_user.id}, {amount} USDT {network}, TXID {txid}. /deposits")
            except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /depositproof AMOUNT TRC20|BEP20 TXID")


@dp.message(Command("transactions"))
async def transactions(message: Message):
    if not await allowed(message): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT tx_id,tx_type,amount,network,status FROM advertiser_transactions WHERE user_id=$1 ORDER BY tx_id DESC LIMIT 15",message.from_user.id)
    await message.answer("\n".join(f"#{r['tx_id']} {r['tx_type']} {r['amount']} {r['network'] or ''} {r['status']}" for r in rows) or "No transactions.")


@dp.message(Command("deposits"))
async def deposits(message: Message):
    if not await is_admin(message.from_user.id): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT tx_id,user_id,amount,network,reference FROM advertiser_transactions WHERE tx_type='deposit' AND status='pending' ORDER BY tx_id DESC LIMIT 20")
    await message.answer("\n\n".join(f"#{r['tx_id']} user {r['user_id']} {r['amount']} {r['network']}\n{r['reference']}" for r in rows) or "No pending deposits.")


@dp.message(Command("approvedeposit"))
async def approve_deposit(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        tid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            row=await c.fetchrow("SELECT * FROM advertiser_transactions WHERE tx_id=$1 AND tx_type='deposit' AND status='pending'",tid)
        if not row: await message.answer("Pending deposit not found."); return
        if not await verify_deposit_on_blockchain(row["network"],row["reference"],row["amount"]):
            await message.answer("Blockchain verification failed. No funds credited."); return
        async with db_pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))",row["reference"].lower())
                current=await c.fetchrow("SELECT * FROM advertiser_transactions WHERE tx_id=$1 AND status='pending' FOR UPDATE",tid)
                if not current: await message.answer("Already processed."); return
                await c.execute("UPDATE advertiser_transactions SET status='approved' WHERE tx_id=$1",tid)
                await c.execute("""INSERT INTO advertiser_wallets(user_id,available) VALUES($1,$2)
                  ON CONFLICT(user_id) DO UPDATE SET available=advertiser_wallets.available+EXCLUDED.available,updated_at=NOW()""",
                  row["user_id"],row["amount"])
        await message.answer(f"Deposit #{tid} verified; {row['amount']} USDT credited.")
        try: await bot.send_message(row["user_id"],f"Deposit verified: {row['amount']} USDT credited.")
        except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /approvedeposit TX_ID")
    except Exception as e:
        print("Deposit approval error:",repr(e)); await message.answer("Verification error; no credit made.")


@dp.message(Command("rejectdeposit"))
async def reject_deposit(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        tid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            r=await c.fetchrow("UPDATE advertiser_transactions SET status='rejected' WHERE tx_id=$1 AND tx_type='deposit' AND status='pending' RETURNING user_id",tid)
        await message.answer("Deposit rejected." if r else "Pending deposit not found.")
    except (ValueError,IndexError): await message.answer("Use: /rejectdeposit ID")


@dp.message(Command("setdepositaddress"))
async def set_deposit_address(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        _,network,address=message.text.split(maxsplit=2); network=network.upper()
        if not valid_wallet(network,address): raise ValueError
        await set_setting("deposit_address_"+network,address)
        await message.answer(f"{network} deposit address saved.")
    except (ValueError,IndexError): await message.answer("Use: /setdepositaddress TRC20|BEP20 ADDRESS")


# ADVERTISER WITHDRAWALS
@dp.message(Command("advwithdraw"))
async def adv_withdraw(message: Message):
    if not await allowed(message): return
    try:
        _,amt,network,wallet=message.text.split(maxsplit=3)
        amount=parse_usdt(amt); network=network.upper()
        if network not in {"TRC20","BEP20"} or not valid_wallet(network,wallet): raise ValueError
        minimum=Decimal(str(await get_setting("adv_min_withdraw","10")))
        if amount<minimum: await message.answer(f"Minimum advertiser withdrawal: {minimum} USDT."); return
        uid=message.from_user.id
        async with db_pool.acquire() as c:
            async with c.transaction():
                w=await c.fetchrow("SELECT available FROM advertiser_wallets WHERE user_id=$1 FOR UPDATE",uid)
                if not w or w["available"]<amount: await message.answer("Insufficient available balance."); return
                await c.execute("UPDATE advertiser_wallets SET available=available-$1,updated_at=NOW() WHERE user_id=$2",amount,uid)
                wid=await c.fetchval("INSERT INTO advertiser_withdrawals(user_id,amount,network,wallet) VALUES($1,$2,$3,$4) RETURNING withdrawal_id",uid,amount,network,wallet)
                await c.execute("INSERT INTO advertiser_transactions(user_id,tx_type,amount,network,reference,status,note) VALUES($1,'withdrawal',$2,$3,$4,'pending',$5)",uid,amount,network,wallet,f"Withdrawal request #{wid}")
        await message.answer(f"Advertiser withdrawal #{wid} submitted. Admin pays manually after review.")
    except (ValueError,IndexError): await message.answer("Use: /advwithdraw AMOUNT TRC20|BEP20 WALLET")


@dp.message(Command("advertiserwithdrawals"))
async def adv_withdrawals(message: Message):
    if not await is_admin(message.from_user.id): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT * FROM advertiser_withdrawals WHERE status IN ('pending','approved') ORDER BY withdrawal_id DESC LIMIT 30")
    await message.answer("\n\n".join(f"#{r['withdrawal_id']} user {r['user_id']} {r['amount']} {r['network']}\n{r['wallet']} | {r['status']} | TXID {r['txid'] or '-'}" for r in rows) or "No advertiser withdrawals.")


@dp.message(Command("approveadvwithdraw"))
async def approve_advwithdraw(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        wid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            async with c.transaction():
                r=await c.fetchrow("UPDATE advertiser_withdrawals SET status='approved' WHERE withdrawal_id=$1 AND status='pending' RETURNING user_id",wid)
                if r: await c.execute("UPDATE advertiser_transactions SET status='approved' WHERE note=$1 AND status='pending'",f"Withdrawal request #{wid}")
        await message.answer(f"Approved. Send payment manually, then /markadvpaid {wid} TXID." if r else "Pending request not found.")
    except (ValueError,IndexError): await message.answer("Use: /approveadvwithdraw ID")


@dp.message(Command("markadvpaid"))
async def mark_advpaid(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        _,wid,txid=message.text.split(maxsplit=2); wid=int(wid)
        if not valid_txid(txid): raise ValueError
        txid=txid.lower().removeprefix("0x")
        async with db_pool.acquire() as c:
            r=await c.fetchrow("UPDATE advertiser_withdrawals SET status='paid',txid=$2 WHERE withdrawal_id=$1 AND status='approved' RETURNING user_id,amount",wid,txid)
        if not r: await message.answer("Approved withdrawal not found, already paid, or TXID used."); return
        async with db_pool.acquire() as c:
            await c.execute("UPDATE advertiser_transactions SET status='paid' WHERE note=$1",f"Withdrawal request #{wid}")
        await message.answer(f"Advertiser withdrawal #{wid} marked paid.")
        try: await bot.send_message(r["user_id"],f"Advertiser withdrawal paid: {r['amount']} USDT. TXID: {txid}")
        except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /markadvpaid ID TXID")


@dp.message(Command("rejectadvwithdraw"))
async def reject_advwithdraw(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        wid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            async with c.transaction():
                r=await c.fetchrow("UPDATE advertiser_withdrawals SET status='rejected' WHERE withdrawal_id=$1 AND status='pending' RETURNING user_id,amount",wid)
                if r:
                    await c.execute("UPDATE advertiser_wallets SET available=available+$1,updated_at=NOW() WHERE user_id=$2",r["amount"],r["user_id"])
                    await c.execute("UPDATE advertiser_transactions SET status='rejected' WHERE note=$1",f"Withdrawal request #{wid}")
        await message.answer("Rejected; funds returned." if r else "Pending withdrawal not found.")
    except (ValueError,IndexError): await message.answer("Use: /rejectadvwithdraw ID")


@dp.message(Command("setadvminwithdraw"))
async def set_adv_min(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        amount=parse_usdt(message.text.split()[1]); await set_setting("adv_min_withdraw",str(amount))
        await message.answer(f"Minimum advertiser withdrawal: {amount} USDT.")
    except (ValueError,IndexError): await message.answer("Use: /setadvminwithdraw AMOUNT")


# CAMPAIGNS
@dp.message(lambda m:m.text in ("📢 Advertise","/advertise"))
async def advertise_menu(message: Message):
    if await allowed(message):
        await message.answer("Create: /createcampaign TITLE | https://t.me/channel | TARGET | REWARD_PER_JOIN_USDT\n"
                             "Campaign budget is reserved until spent or refunded.")


async def get_public_chat(link):
    if not link.startswith("https://t.me/") or link.startswith("https://t.me/+"):
        raise ValueError("Use a public Telegram channel link")
    username=link.rstrip("/").split("/")[-1].split("?")[0]
    if not username or username.startswith("+"): raise ValueError("Invalid link")
    chat=await bot.get_chat("@"+username)
    if chat.type not in ("channel","supergroup"): raise ValueError("Not a channel/group")
    me=await bot.get_me()
    member=await bot.get_chat_member(chat.id,me.id)
    if member.status not in ("administrator","creator"): raise ValueError("Bot must be admin")
    return chat


@dp.message(Command("createcampaign"))
async def create_campaign(message: Message):
    if not await allowed(message): return
    try:
        parts=[x.strip() for x in message.text.split(maxsplit=1)[1].split("|")]
        if len(parts)!=4: raise ValueError
        title,link,target_s,reward_s=parts
        target=int(target_s); reward=parse_usdt(reward_s)
        if not title or target<1 or target>100000: raise ValueError
        chat=await get_public_chat(link)
        budget=(reward*target).quantize(Decimal("0.00000001"))
        if budget<1: await message.answer("Campaign budget must be at least 1 USDT."); return
        uid=message.from_user.id
        async with db_pool.acquire() as c:
            async with c.transaction():
                w=await c.fetchrow("SELECT available FROM advertiser_wallets WHERE user_id=$1 FOR UPDATE",uid)
                if not w or w["available"]<budget: await message.answer(f"Insufficient funds. Required: {budget} USDT."); return
                await c.execute("UPDATE advertiser_wallets SET available=available-$1,reserved=reserved+$1,updated_at=NOW() WHERE user_id=$2",budget,uid)
                cid=await c.fetchval("""INSERT INTO campaigns(advertiser_id,title,channel_link,target_members,reward_per_join,total_budget,chat_id,status,is_test,budget_reserved)
                  VALUES($1,$2,$3,$4,$5,$6,$7,'pending_payment',FALSE,TRUE) RETURNING campaign_id""",
                  uid,title,link,target,reward,str(budget),str(chat.id))
        await message.answer(f"Campaign #{cid} created; {budget} USDT reserved. Awaiting admin approval.")
        for aid in ADMIN_IDS:
            try: await bot.send_message(aid,f"Campaign #{cid}: {title}\nAdvertiser {uid}\n{link}\nBudget {budget}\n/campaigns")
            except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /createcampaign TITLE | https://t.me/channel | TARGET | REWARD")
    except Exception as e:
        print("Create campaign error:",repr(e)); await message.answer("Could not validate channel. Check public link and bot admin rights.")


@dp.message(Command("testcampaign"))
async def test_campaign(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        link=message.text.split(maxsplit=1)[1].strip()
        chat=await get_public_chat(link)
        async with db_pool.acquire() as c:
            cid=await c.fetchval("""INSERT INTO campaigns(advertiser_id,title,channel_link,target_members,reward_per_join,total_budget,chat_id,status,is_test,budget_reserved)
              VALUES($1,$2,$3,5,0,0,$4,'active',TRUE,FALSE) RETURNING campaign_id""",
              message.from_user.id,"TEST: "+chat.title,link,str(chat.id))
        await message.answer(f"Test campaign #{cid} created. Membership verification only; no redeemable points are awarded.\n/joincampaign {cid}")
    except (ValueError,IndexError): await message.answer("Use: /testcampaign https://t.me/ChannelName")
    except Exception as e:
        print("Test campaign error:",repr(e)); await message.answer("Could not create test campaign; check bot admin rights.")


@dp.message(Command("mycampaigns"))
async def my_campaigns(message: Message):
    if not await allowed(message): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT campaign_id,title,target_members,verified_joins,total_budget,spent,status FROM campaigns WHERE advertiser_id=$1 ORDER BY campaign_id DESC LIMIT 20",message.from_user.id)
    await message.answer("\n\n".join(f"#{r['campaign_id']} {r['title']}\n{r['status']} {r['verified_joins']}/{r['target_members']}\nSpent {r['spent']}/{r['total_budget']} USDT" for r in rows) or "No campaigns.")


@dp.message(Command("campaigns"))
async def campaigns(message: Message):
    if not await is_admin(message.from_user.id): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT campaign_id,advertiser_id,title,channel_link,target_members,verified_joins,reward_per_join,total_budget,status FROM campaigns WHERE status IN ('pending_payment','pending_review','active') ORDER BY campaign_id DESC LIMIT 25")
    await message.answer("\n\n".join(f"#{r['campaign_id']} user {r['advertiser_id']} {r['title']}\n{r['channel_link']}\n{r['verified_joins']}/{r['target_members']} reward {r['reward_per_join']} budget {r['total_budget']} status {r['status']}\n/approvecampaign {r['campaign_id']} | /rejectcampaign {r['campaign_id']}" for r in rows) or "No campaigns.")


@dp.message(Command("approvecampaign"))
async def approve_campaign(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        cid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            r=await c.fetchrow("UPDATE campaigns SET status='active',updated_at=NOW() WHERE campaign_id=$1 AND status='pending_payment' AND is_test=FALSE RETURNING advertiser_id,title",cid)
        await message.answer("Campaign approved and active." if r else "Pending campaign not found.")
        if r:
            try: await bot.send_message(r["advertiser_id"],f"Campaign #{cid} ({r['title']}) approved.")
            except Exception: pass
    except (ValueError,IndexError): await message.answer("Use: /approvecampaign ID")


@dp.message(Command("rejectcampaign"))
async def reject_campaign(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        cid=int(message.text.split()[1])
        async with db_pool.acquire() as c:
            async with c.transaction():
                r=await c.fetchrow("SELECT advertiser_id,total_budget,budget_reserved FROM campaigns WHERE campaign_id=$1 AND status IN ('pending_payment','pending_review') FOR UPDATE",cid)
                if r:
                    await c.execute("UPDATE campaigns SET status='rejected',budget_reserved=FALSE,updated_at=NOW() WHERE campaign_id=$1",cid)
                    if r["budget_reserved"]:
                        w=await c.fetchrow("SELECT reserved FROM advertiser_wallets WHERE user_id=$1 FOR UPDATE",r["advertiser_id"])
                        if not w or w["reserved"]<r["total_budget"]:
                            raise RuntimeError("Reserved wallet balance mismatch; manual reconciliation required")
                        await c.execute("UPDATE advertiser_wallets SET available=available+$1,reserved=reserved-$1,updated_at=NOW() WHERE user_id=$2",r["total_budget"],r["advertiser_id"])
        await message.answer("Campaign rejected and budget refunded." if r else "Campaign not found.")
    except (ValueError,IndexError): await message.answer("Use: /rejectcampaign ID")
    except Exception as e: print("Reject campaign error:",repr(e)); await message.answer("Refund stopped due to a wallet accounting mismatch. Contact support.")


@dp.message(Command("joincampaign"))
async def join_campaign(message: Message):
    if not await allowed(message): return
    try:
        cid=int(message.text.split()[1]); uid=message.from_user.id
        async with db_pool.acquire() as c:
            camp=await c.fetchrow("SELECT campaign_id,title,channel_link,chat_id,target_members,verified_joins FROM campaigns WHERE campaign_id=$1 AND status='active'",cid)
            if not camp: await message.answer("Campaign not found or inactive."); return
            if camp["verified_joins"]>=camp["target_members"]: await message.answer("Campaign target reached."); return
            await c.execute("INSERT INTO campaign_joins(campaign_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",cid,uid)
            status=await c.fetchval("SELECT status FROM campaign_joins WHERE campaign_id=$1 AND user_id=$2",cid,uid)
        if status=="approved": await message.answer("Already verified."); return
        if status=="rejected": await message.answer("Previous request rejected."); return
        await message.answer(f"{camp['title']}\nJoin: {camp['channel_link']}\nThen use /verifycampaign {cid}")
    except (ValueError,IndexError): await message.answer("Use: /joincampaign ID")


@dp.message(Command("verifycampaign"))
async def verify_campaign(message: Message):
    if not await allowed(message): return
    try:
        cid=int(message.text.split()[1]); uid=message.from_user.id
        async with db_pool.acquire() as c:
            camp=await c.fetchrow("SELECT chat_id FROM campaigns WHERE campaign_id=$1 AND status='active'",cid)
            join=await c.fetchrow("SELECT status FROM campaign_joins WHERE campaign_id=$1 AND user_id=$2",cid,uid)
        if not camp or not join: await message.answer("Campaign/join request not found."); return
        if join["status"]=="approved": await message.answer("Already rewarded."); return
        if join["status"]!="pending": await message.answer("Join request is not pending."); return
        try:
            member=await bot.get_chat_member(camp["chat_id"],uid)
            is_member=member.status in ("member","administrator","creator") or (member.status=="restricted" and getattr(member,"is_member",False))
        except Exception as e:
            print("Membership API error:",repr(e)); await message.answer("Could not check membership; try again later."); return
        if not is_member: await message.answer("Join the channel first, then retry."); return

        async with db_pool.acquire() as c:
            async with c.transaction():
                campaign=await c.fetchrow("SELECT * FROM campaigns WHERE campaign_id=$1 AND status='active' FOR UPDATE",cid)
                j=await c.fetchrow("SELECT * FROM campaign_joins WHERE campaign_id=$1 AND user_id=$2 FOR UPDATE",cid,uid)
                if not campaign or not j or j["status"]!="pending": await message.answer("Request already processed."); return
                if campaign["verified_joins"]>=campaign["target_members"]: await message.answer("Campaign target reached."); return
                reward=campaign["reward_per_join"]
                spent=campaign["spent"]+reward
                if spent>campaign["total_budget"]: await message.answer("Campaign budget exhausted."); return
                points=0 if campaign["is_test"] else int((reward*POINTS_PER_USDT).to_integral_value(rounding=ROUND_DOWN))
                if not campaign["is_test"] and points<1: await message.answer("Campaign reward is too small to convert to points."); return
                if not campaign["is_test"]:
                    w=await c.fetchrow("SELECT reserved FROM advertiser_wallets WHERE user_id=$1 FOR UPDATE",campaign["advertiser_id"])
                    if not w or w["reserved"]<reward: raise RuntimeError("Reserved wallet balance mismatch")
                await c.execute("UPDATE campaign_joins SET status='approved' WHERE join_id=$1",j["join_id"])
                await c.execute("UPDATE campaigns SET verified_joins=verified_joins+1,spent=spent+$1,updated_at=NOW() WHERE campaign_id=$2",reward,cid)
                if not campaign["is_test"]:
                    await c.execute("UPDATE advertiser_wallets SET reserved=reserved-$1,updated_at=NOW() WHERE user_id=$2",reward,campaign["advertiser_id"])
                    await c.execute("UPDATE users SET points=points+$1 WHERE user_id=$2",points,uid)
                if campaign["verified_joins"]+1>=campaign["target_members"]:
                    remaining=campaign["total_budget"]-spent
                    await c.execute("UPDATE campaigns SET status='completed',budget_reserved=FALSE,updated_at=NOW() WHERE campaign_id=$1",cid)
                    if not campaign["is_test"] and campaign["budget_reserved"] and remaining>0:
                        w=await c.fetchrow("SELECT reserved FROM advertiser_wallets WHERE user_id=$1 FOR UPDATE",campaign["advertiser_id"])
                        if not w or w["reserved"]<remaining: raise RuntimeError("Reserved balance mismatch on campaign completion")
                        await c.execute("UPDATE advertiser_wallets SET available=available+$1,reserved=reserved-$1,updated_at=NOW() WHERE user_id=$2",remaining,campaign["advertiser_id"])
        await message.answer("✅ Test membership verified; no points awarded." if campaign["is_test"] else f"✅ Membership verified! +{points} points. Campaign #{cid}")
    except (ValueError,IndexError): await message.answer("Use: /verifycampaign ID")
    except Exception as e: print("Campaign verify error:",repr(e)); await message.answer("Verification failed safely. Please contact admin.")


@dp.message(Command("setpointsrate"))
async def set_points_rate(message: Message):
    if not await is_admin(message.from_user.id): return
    try:
        rate=Decimal(message.text.split()[1])
        if rate!=POINTS_PER_USDT: raise ValueError
        await set_setting("points_per_usdt","1000")
        await message.answer("Rate fixed at 1,000 points = 1 USDT.")
    except (ValueError,IndexError,InvalidOperation): await message.answer("This version fixes the conversion at 1,000 points = 1 USDT.")


# MENU
@dp.message(lambda m:m.text=="🎬 Watch Ads")
async def watch_ads(message: Message):
    if not await allowed(message): return
    # No real rewarded-ad provider is integrated. Do not create withdrawable liabilities.
    await message.answer("Real rewarded ads are not connected yet. No points were added. This feature will activate after an ad provider is integrated.")


@dp.message(lambda m:m.text=="💰 Balance")
async def balance(message: Message):
    if not await allowed(message): return
    u=await get_user(message.from_user.id)
    await message.answer(f"Your balance: {u['points']} points (${u['points']/1000:.3f} equivalent; minimum withdrawal 5,000 points).")


@dp.message(lambda m:m.text=="📋 Tasks")
async def tasks(message: Message):
    if not await allowed(message): return
    async with db_pool.acquire() as c:
        rows=await c.fetch("SELECT task_id,title,reward FROM tasks WHERE active=TRUE ORDER BY task_id DESC")
        camps=await c.fetch("SELECT campaign_id,title,target_members,verified_joins,reward_per_join,is_test FROM campaigns WHERE status='active' AND verified_joins<target_members ORDER BY campaign_id DESC LIMIT 15")
    out="📋 AVAILABLE TASKS\n\n"+"\n\n".join(f"#{r['task_id']} {r['title']}\nReward: {r['reward']} points\n/submit {r['task_id']} YOUR_PROOF" for r in rows)
    if camps:
        out+="\n\n📢 CAMPAIGNS\n"+"\n\n".join(f"#{r['campaign_id']} {r['title']} {r['verified_joins']}/{r['target_members']}\nReward: {'TEST - no points' if r['is_test'] else str(r['reward_per_join'])+' USDT equivalent'}\n/joincampaign {r['campaign_id']}" for r in camps)
    await message.answer(out if rows or camps else "No tasks available.")


@dp.message(lambda m:m.text=="💳 Withdraw")
async def withdraw_menu(message: Message):
    if await allowed(message):
        await message.answer(f"Minimum: {await get_setting('min_withdraw',MIN_WITHDRAWAL)} points ($5).\nUse /withdraw AMOUNT TRC20|BEP20 WALLET_ADDRESS\nHistory: /mywithdrawals")


@dp.message()
async def fallback(message: Message):
    await message.answer("Please use the menu buttons below. 👇",reply_markup=keyboard)


async def health_handler(request):
    return web.Response(text="EarnWave bot is running")


async def start_web_server():
    app=web.Application()
    app.router.add_get("/",health_handler)
    app.router.add_get("/health",health_handler)
    runner=web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",PORT).start()
    return runner


async def main():
    await setup_database()
    runner=await start_web_server()
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await db_pool.close()
        await runner.cleanup()


if __name__=="__main__":
    asyncio.run(main())
