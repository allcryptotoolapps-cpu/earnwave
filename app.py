"""EarnWave Telegram Mini App API + existing aiogram bot launcher.
Keep the existing main.py in the same directory. Start with: python app.py
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram.types import (
    KeyboardButton, ReplyKeyboardMarkup, WebAppInfo,
    MenuButtonWebApp, InlineKeyboardMarkup, InlineKeyboardButton,
)

import main as core

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip().rstrip("/")
TOKEN = os.getenv("BOT_TOKEN", "")


def json_response(data, status=200):
    return web.json_response(data, status=status)


def parse_amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise web.HTTPBadRequest(text="Invalid amount")
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -8:
        raise web.HTTPBadRequest(text="Amount must be positive, max 8 decimals")
    return amount.quantize(Decimal("0.00000001"))


def telegram_user_from_init_data(init_data):
    if not init_data or not TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = int(pairs.get("auth_date", "0"))
        if auth_date <= 0 or time.time() - auth_date > 86400 or auth_date - time.time() > 60:
            return None
        user = json.loads(pairs.get("user", "{}"))
        uid = int(user["id"])
        return {"id": uid, "first_name": str(user.get("first_name", "User"))[:100],
                "last_name": str(user.get("last_name", ""))[:100],
                "username": str(user.get("username", ""))[:100],
                "language_code": str(user.get("language_code", "en"))[:10]}
    except Exception:
        return None


@web.middleware
async def auth_middleware(request, handler):
    if request.path.startswith("/api/"):
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        user = telegram_user_from_init_data(init_data)
        if not user:
            return json_response({"error": "Telegram session invalid or expired. Open the app from Telegram."}, 401)
        request["tg_user"] = user
        if user["id"] not in core.ADMIN_IDS:
            existing = await core.get_user(user["id"])
            if not existing:
                await core.register_user(user["id"])
                existing = await core.get_user(user["id"])
            if existing and existing["is_banned"]:
                return json_response({"error": "Your account is banned."}, 403)
    return await handler(request)


async def body_json(request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:
        raise web.HTTPBadRequest(text="Invalid JSON body")


async def api_me(request):
    u = request["tg_user"]
    uid = u["id"]
    await core.register_user(uid)
    async with core.db_pool.acquire() as c:
        row = await c.fetchrow("SELECT points,language,is_banned,referred_by FROM users WHERE user_id=$1", uid)
        referrals = await c.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1", uid)
        wallet = await c.fetchrow("SELECT available,reserved FROM advertiser_wallets WHERE user_id=$1", uid)
        task_count = await c.fetchval("SELECT COUNT(*) FROM tasks WHERE active=TRUE")
        campaign_count = await c.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='active' AND verified_joins<target_members")
        paid_refs = await c.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1 AND referral_paid=TRUE", uid)
    bot_info = await core.bot.get_me()
    bonus = await core.get_setting("referral_bonus", core.REFERRAL_BONUS)
    return json_response({
        "user": u, "points": int(row["points"]), "usdt_value": f"{Decimal(row['points']) / Decimal(1000):.3f}",
        "language": row["language"], "is_admin": uid in core.ADMIN_IDS,
        "min_withdraw": int(await core.get_setting("min_withdraw", core.MIN_WITHDRAWAL)),
        "referrals": int(referrals), "referral_bonus": int(bonus), "referral_link": f"https://t.me/{bot_info.username}?start=ref_{uid}",
        "wallet": {"available": f"{wallet['available']:.2f}", "reserved": f"{wallet['reserved']:.2f}"} if wallet else {"available":"0.00","reserved":"0.00"},
        "task_count": int(task_count), "campaign_count": int(campaign_count), "paid_referrals": int(paid_refs)
    })


async def api_tasks(request):
    uid = request["tg_user"]["id"]
    async with core.db_pool.acquire() as c:
        rows = await c.fetch("""SELECT t.task_id,t.title,t.reward,
          (SELECT s.status FROM submissions s WHERE s.task_id=t.task_id AND s.user_id=$1 ORDER BY s.submission_id DESC LIMIT 1) AS my_status
          FROM tasks t WHERE t.active=TRUE ORDER BY t.task_id DESC""", uid)
    return json_response({"tasks": [{"id":r["task_id"],"title":r["title"],"reward":r["reward"],"status":r["my_status"]} for r in rows]})


async def api_campaigns(request):
    uid = request["tg_user"]["id"]
    async with core.db_pool.acquire() as c:
        rows = await c.fetch("""SELECT c.campaign_id,c.title,c.channel_link,c.target_members,c.verified_joins,
          c.reward_per_join,c.is_test,j.status AS my_status FROM campaigns c
          LEFT JOIN campaign_joins j ON j.campaign_id=c.campaign_id AND j.user_id=$1
          WHERE c.status='active' AND c.verified_joins<c.target_members ORDER BY c.campaign_id DESC LIMIT 50""", uid)
    return json_response({"campaigns":[{"id":r["campaign_id"],"title":r["title"],"link":r["channel_link"],"target":r["target_members"],"joined":r["verified_joins"],"reward":str(r["reward_per_join"]),"is_test":r["is_test"],"status":r["my_status"]} for r in rows]})


async def api_submit_task(request):
    uid=request["tg_user"]["id"]; data=await body_json(request)
    try: tid=int(data.get("task_id")); proof=str(data.get("proof","")).strip()
    except Exception: raise web.HTTPBadRequest(text="Invalid task")
    if not proof or len(proof)>2000: raise web.HTTPBadRequest(text="Proof is required (max 2000 characters)")
    async with core.db_pool.acquire() as c:
        async with c.transaction():
            exists=await c.fetchval("SELECT 1 FROM tasks WHERE task_id=$1 AND active=TRUE",tid)
            if not exists: raise web.HTTPNotFound(text="Task not found")
            sid=await c.fetchval("""INSERT INTO submissions(user_id,task_id,proof) VALUES($1,$2,$3)
              ON CONFLICT(user_id,task_id) WHERE status IN ('pending','approved') DO NOTHING RETURNING submission_id""",uid,tid,proof)
    if not sid: return json_response({"ok":False,"message":"You already submitted this task."},409)
    return json_response({"ok":True,"submission_id":sid,"message":"Task proof submitted."})


async def api_join_campaign(request):
    uid=request["tg_user"]["id"]; cid=int(request.match_info["cid"])
    async with core.db_pool.acquire() as c:
        camp=await c.fetchrow("SELECT campaign_id,title,channel_link,target_members,verified_joins FROM campaigns WHERE campaign_id=$1 AND status='active'",cid)
        if not camp: raise web.HTTPNotFound(text="Campaign not found")
        if camp["verified_joins"]>=camp["target_members"]: raise web.HTTPConflict(text="Campaign target reached")
        await c.execute("INSERT INTO campaign_joins(campaign_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",cid,uid)
        status=await c.fetchval("SELECT status FROM campaign_joins WHERE campaign_id=$1 AND user_id=$2",cid,uid)
    return json_response({"ok":True,"status":status,"link":camp["channel_link"],"message":"Join the channel, then return and tap Verify."})


async def api_verify_campaign(request):
    uid=request["tg_user"]["id"]; cid=int(request.match_info["cid"])
    async with core.db_pool.acquire() as c:
        camp=await c.fetchrow("SELECT * FROM campaigns WHERE campaign_id=$1 AND status='active'",cid)
        join=await c.fetchrow("SELECT * FROM campaign_joins WHERE campaign_id=$1 AND user_id=$2",cid,uid)
    if not camp or not join: raise web.HTTPNotFound(text="Campaign/join request not found")
    if join["status"]=="approved": return json_response({"ok":True,"message":"Already verified."})
    if join["status"]!="pending": raise web.HTTPConflict(text="Join request is not pending")
    try:
        member=await core.bot.get_chat_member(camp["chat_id"],uid)
        is_member=member.status in ("member","administrator","creator") or (member.status=="restricted" and getattr(member,"is_member",False))
    except Exception:
        raise web.HTTPBadGateway(text="Telegram could not verify membership. Try again later.")
    if not is_member: raise web.HTTPForbidden(text="Join the channel first, then retry.")
    async with core.db_pool.acquire() as c:
        async with c.transaction():
            campaign=await c.fetchrow("SELECT * FROM campaigns WHERE campaign_id=$1 AND status='active' FOR UPDATE",cid)
            j=await c.fetchrow("SELECT * FROM campaign_joins WHERE campaign_id=$1 AND user_id=$2 FOR UPDATE",cid,uid)
            if not campaign or not j or j["status"]!="pending": raise web.HTTPConflict(text="Request already processed")
            if campaign["verified_joins"]>=campaign["target_members"]: raise web.HTTPConflict(text="Campaign target reached")
            reward=campaign["reward_per_join"]; spent=campaign["spent"]+reward
            if spent>campaign["total_budget"]: raise web.HTTPConflict(text="Campaign budget exhausted")
            points=0 if campaign["is_test"] else int((reward*core.POINTS_PER_USDT).to_integral_value(rounding=core.ROUND_DOWN))
            if not campaign["is_test"] and points<1: raise web.HTTPBadRequest(text="Reward is too small")
            if not campaign["is_test"]:
                w=await c.fetchrow("SELECT reserved FROM advertiser_wallets WHERE user_id=$1 FOR UPDATE",campaign["advertiser_id"])
                if not w or w["reserved"]<reward: raise web.HTTPConflict(text="Campaign accounting needs admin review")
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
                    if not w or w["reserved"]<remaining: raise web.HTTPConflict(text="Campaign refund accounting mismatch")
                    await c.execute("UPDATE advertiser_wallets SET available=available+$1,reserved=reserved-$1,updated_at=NOW() WHERE user_id=$2",remaining,campaign["advertiser_id"])
    return json_response({"ok":True,"points_awarded":points,"message":"Test membership verified; no points awarded." if campaign["is_test"] else f"Membership verified! +{points} points."})


async def api_wallet(request):
    uid=request["tg_user"]["id"]; w=await core.ensure_wallet(uid)
    return json_response({"available":f"{w['available']:.2f}","reserved":f"{w['reserved']:.2f}",
      "min_withdraw":str(await core.get_setting("adv_min_withdraw","5")),
      "trc20":str(await core.get_setting("deposit_address_TRC20",core.USDT_TRC20_ADDRESS) or ""),
      "bep20":str(await core.get_setting("deposit_address_BEP20",core.USDT_BEP20_ADDRESS) or "")})


async def api_history(request):
    uid=request["tg_user"]["id"]
    async with core.db_pool.acquire() as c:
        w=await c.fetch("SELECT withdrawal_id,amount,wallet,status,txid,created_at FROM withdrawals WHERE user_id=$1 ORDER BY withdrawal_id DESC LIMIT 15",uid)
        a=await c.fetch("SELECT tx_id,tx_type,amount,network,status,created_at FROM advertiser_transactions WHERE user_id=$1 ORDER BY tx_id DESC LIMIT 15",uid)
    items=[]
    for r in w: items.append({"id":r["withdrawal_id"],"type":"Points withdrawal","amount":f"{r['amount']} points","network":r["wallet"].split("|",1)[0],"status":r["status"],"txid":r["txid"] or "","created_at":r["created_at"].isoformat()})
    for r in a: items.append({"id":r["tx_id"],"type":r["tx_type"],"amount":f"{r['amount']:.2f} USDT","network":r["network"] or "","status":r["status"],"txid":"","created_at":r["created_at"].isoformat()})
    items.sort(key=lambda x:x["created_at"],reverse=True)
    return json_response({"items":items[:25]})


async def api_withdraw(request):
    uid=request["tg_user"]["id"]; data=await body_json(request)
    try: amount=int(data.get("points")); network=str(data.get("network","")).upper(); wallet=str(data.get("wallet","")).strip()
    except Exception: raise web.HTTPBadRequest(text="Invalid withdrawal details")
    if amount<=0 or network not in {"TRC20","BEP20"} or not core.valid_wallet(network,wallet): raise web.HTTPBadRequest(text="Invalid network or wallet address")
    minimum=max(5000,int(await core.get_setting("min_withdraw",core.MIN_WITHDRAWAL)))
    if amount<minimum: raise web.HTTPBadRequest(text=f"Minimum withdrawal is {minimum} points")
    async with core.db_pool.acquire() as c:
        async with c.transaction():
            user=await c.fetchrow("SELECT points FROM users WHERE user_id=$1 FOR UPDATE",uid)
            if not user or user["points"]<amount: raise web.HTTPBadRequest(text="Insufficient points")
            await c.execute("UPDATE users SET points=points-$1 WHERE user_id=$2",amount,uid)
            wid=await c.fetchval("INSERT INTO withdrawals(user_id,amount,wallet) VALUES($1,$2,$3) RETURNING withdrawal_id",uid,amount,f"{network}|{wallet}")
    return json_response({"ok":True,"id":wid,"message":"Withdrawal request submitted. Admin processes payment manually."})


async def api_advwithdraw(request):
    uid=request["tg_user"]["id"]; data=await body_json(request)
    amount=parse_amount(data.get("amount")); network=str(data.get("network","")).upper(); wallet=str(data.get("wallet","")).strip()
    if network not in {"TRC20","BEP20"} or not core.valid_wallet(network,wallet): raise web.HTTPBadRequest(text="Invalid network or wallet address")
    minimum=Decimal(str(await core.get_setting("adv_min_withdraw","5")))
    if amount<minimum: raise web.HTTPBadRequest(text=f"Minimum advertiser withdrawal is {minimum} USDT")
    async with core.db_pool.acquire() as c:
        async with c.transaction():
            w=await c.fetchrow("SELECT available FROM advertiser_wallets WHERE user_id=$1 FOR UPDATE",uid)
            if not w or w["available"]<amount: raise web.HTTPBadRequest(text="Insufficient advertiser balance")
            await c.execute("UPDATE advertiser_wallets SET available=available-$1,updated_at=NOW() WHERE user_id=$2",amount,uid)
            wid=await c.fetchval("INSERT INTO advertiser_withdrawals(user_id,amount,network,wallet) VALUES($1,$2,$3,$4) RETURNING withdrawal_id",uid,amount,network,wallet)
            await c.execute("INSERT INTO advertiser_transactions(user_id,tx_type,amount,network,reference,status,note) VALUES($1,'withdrawal',$2,$3,$4,'pending',$5)",uid,amount,network,wallet,f"Withdrawal request #{wid}")
    return json_response({"ok":True,"id":wid,"message":"Advertiser withdrawal request submitted."})


async def api_deposit_proof(request):
    uid=request["tg_user"]["id"]; data=await body_json(request)
    amount=parse_amount(data.get("amount")); network=str(data.get("network","")).upper(); txid=str(data.get("txid","")).strip()
    if network not in {"TRC20","BEP20"} or not core.valid_txid(txid): raise web.HTTPBadRequest(text="Invalid network or transaction hash")
    txid=txid.lower().removeprefix("0x")
    async with core.db_pool.acquire() as c:
        tx=await c.fetchval("""INSERT INTO advertiser_transactions(user_id,tx_type,amount,network,reference,note)
          VALUES($1,'deposit',$2,$3,$4,'Mini App deposit proof') ON CONFLICT DO NOTHING RETURNING tx_id""",uid,amount,network,txid)
    if not tx: raise web.HTTPConflict(text="This TXID has already been submitted")
    for aid in core.ADMIN_IDS:
        try: await core.bot.send_message(aid,f"Mini App deposit #{tx}: user {uid}, {amount} USDT {network}, TXID {txid}. /deposits")
        except Exception: pass
    return json_response({"ok":True,"id":tx,"message":"Deposit proof submitted for on-chain verification."})


# Admin API endpoints
async def require_admin(request):
    if request["tg_user"]["id"] not in core.ADMIN_IDS: raise web.HTTPForbidden(text="Admin access required")
    return request["tg_user"]["id"]

async def admin_summary(request):
    await require_admin(request)
    async with core.db_pool.acquire() as c:
        data={"users":await c.fetchval("SELECT COUNT(*) FROM users"),"points":await c.fetchval("SELECT COALESCE(SUM(points),0) FROM users"),
          "pending_withdrawals":await c.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status='pending'"),
          "pending_deposits":await c.fetchval("SELECT COUNT(*) FROM advertiser_transactions WHERE tx_type='deposit' AND status='pending'"),
          "pending_submissions":await c.fetchval("SELECT COUNT(*) FROM submissions WHERE status='pending'"),
          "pending_campaigns":await c.fetchval("SELECT COUNT(*) FROM campaigns WHERE status IN ('pending_payment','pending_review')")}
    return json_response(data)

async def admin_list(request):
    await require_admin(request); kind=request.match_info["kind"]
    async with core.db_pool.acquire() as c:
        if kind=="users": rows=await c.fetch("SELECT user_id,points,is_banned FROM users ORDER BY user_id DESC LIMIT 100")
        elif kind=="submissions": rows=await c.fetch("SELECT submission_id,user_id,task_id,proof,status FROM submissions WHERE status='pending' ORDER BY submission_id DESC LIMIT 100")
        elif kind=="withdrawals": rows=await c.fetch("SELECT withdrawal_id,user_id,amount,wallet,status,txid FROM withdrawals WHERE status IN ('pending','approved') ORDER BY withdrawal_id DESC LIMIT 100")
        elif kind=="deposits": rows=await c.fetch("SELECT tx_id,user_id,amount,network,reference,status FROM advertiser_transactions WHERE tx_type='deposit' AND status='pending' ORDER BY tx_id DESC LIMIT 100")
        elif kind=="campaigns": rows=await c.fetch("SELECT campaign_id,advertiser_id,title,channel_link,total_budget,status FROM campaigns WHERE status IN ('pending_payment','pending_review','active') ORDER BY campaign_id DESC LIMIT 100")
        elif kind=="advwithdrawals": rows=await c.fetch("SELECT withdrawal_id,user_id,amount,network,wallet,status,txid FROM advertiser_withdrawals WHERE status IN ('pending','approved') ORDER BY withdrawal_id DESC LIMIT 100")
        else: raise web.HTTPNotFound(text="Unknown list")
    result=[]
    for r in rows: result.append({k:(str(v) if isinstance(v,Decimal) else v.isoformat() if hasattr(v,"isoformat") else v) for k,v in dict(r).items()})
    return json_response({"items":result})

async def admin_action(request):
    await require_admin(request); data=await body_json(request); action=request.match_info["action"]
    try: item_id=int(data.get("id"))
    except Exception: raise web.HTTPBadRequest(text="Invalid ID")
    async with core.db_pool.acquire() as c:
        async with c.transaction():
            if action in ("approve-submission","reject-submission"):
                r=await c.fetchrow("SELECT s.user_id,t.reward FROM submissions s JOIN tasks t USING(task_id) WHERE s.submission_id=$1 AND s.status='pending' FOR UPDATE OF s",item_id)
                if not r: raise web.HTTPNotFound(text="Pending submission not found")
                status="approved" if action.startswith("approve") else "rejected"
                await c.execute("UPDATE submissions SET status=$1 WHERE submission_id=$2",status,item_id)
                if status=="approved": await c.execute("UPDATE users SET points=points+$1 WHERE user_id=$2",r["reward"],r["user_id"])
            elif action in ("approve-withdrawal","reject-withdrawal"):
                status="approved" if action.startswith("approve") else "rejected"
                r=await c.fetchrow("UPDATE withdrawals SET status=$1 WHERE withdrawal_id=$2 AND status='pending' RETURNING user_id,amount",status,item_id)
                if not r: raise web.HTTPNotFound(text="Pending withdrawal not found")
                if status=="rejected": await c.execute("UPDATE users SET points=points+$1 WHERE user_id=$2",r["amount"],r["user_id"])
            elif action=="mark-withdrawal-paid":
                txid=str(data.get("txid","")).strip()
                if not core.valid_txid(txid): raise web.HTTPBadRequest(text="Invalid 64-character TXID")
                txid=txid.lower().removeprefix("0x")
                r=await c.fetchrow("UPDATE withdrawals SET status='paid',txid=$1 WHERE withdrawal_id=$2 AND status='approved' RETURNING user_id",txid,item_id)
                if not r: raise web.HTTPNotFound(text="Approved withdrawal not found or TXID already used")
            elif action in ("approve-campaign","reject-campaign"):
                if action=="approve-campaign":
                    r=await c.fetchrow("UPDATE campaigns SET status='active',updated_at=NOW() WHERE campaign_id=$1 AND status='pending_payment' AND is_test=FALSE RETURNING campaign_id",item_id)
                    if not r: raise web.HTTPNotFound(text="Pending campaign not found")
                else:
                    r=await c.fetchrow("SELECT advertiser_id,total_budget,budget_reserved FROM campaigns WHERE campaign_id=$1 AND status IN ('pending_payment','pending_review') FOR UPDATE",item_id)
                    if not r: raise web.HTTPNotFound(text="Pending campaign not found")
                    await c.execute("UPDATE campaigns SET status='rejected',budget_reserved=FALSE,updated_at=NOW() WHERE campaign_id=$1",item_id)
                    if r["budget_reserved"]:
                        w=await c.fetchrow("SELECT reserved FROM advertiser_wallets WHERE user_id=$1 FOR UPDATE",r["advertiser_id"])
                        if not w or w["reserved"]<r["total_budget"]: raise web.HTTPConflict(text="Reserved balance mismatch")
                        await c.execute("UPDATE advertiser_wallets SET available=available+$1,reserved=reserved-$1,updated_at=NOW() WHERE user_id=$2",r["total_budget"],r["advertiser_id"])
            else: raise web.HTTPNotFound(text="Unknown action")
    return json_response({"ok":True,"message":"Action completed."})


async def admin_deposit_action(request):
    await require_admin(request); data=await body_json(request)
    try: tid=int(data.get("id"))
    except Exception: raise web.HTTPBadRequest(text="Invalid deposit ID")
    async with core.db_pool.acquire() as c:
        row=await c.fetchrow("SELECT * FROM advertiser_transactions WHERE tx_id=$1 AND tx_type='deposit' AND status='pending'",tid)
    if not row: raise web.HTTPNotFound(text="Pending deposit not found")
    if data.get("action")=="reject":
        async with core.db_pool.acquire() as c: await c.execute("UPDATE advertiser_transactions SET status='rejected' WHERE tx_id=$1 AND status='pending'",tid)
        return json_response({"ok":True,"message":"Deposit rejected."})
    try:
        verified=await core.verify_deposit_on_blockchain(row["network"],row["reference"],row["amount"])
    except Exception as e:
        print("Mini App deposit verification error:",repr(e)); raise web.HTTPBadGateway(text="Blockchain verification unavailable; no funds credited")
    if not verified: raise web.HTTPBadRequest(text="Blockchain verification failed; no funds credited")
    async with core.db_pool.acquire() as c:
        async with c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))",row["reference"].lower())
            current=await c.fetchrow("SELECT * FROM advertiser_transactions WHERE tx_id=$1 AND status='pending' FOR UPDATE",tid)
            if not current: raise web.HTTPConflict(text="Deposit already processed")
            await c.execute("UPDATE advertiser_transactions SET status='approved' WHERE tx_id=$1",tid)
            await c.execute("INSERT INTO advertiser_wallets(user_id,available) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET available=advertiser_wallets.available+EXCLUDED.available,updated_at=NOW()",row["user_id"],row["amount"])
    return json_response({"ok":True,"message":f"Verified and credited {row['amount']} USDT."})


async def admin_advwithdraw_action(request):
    await require_admin(request); data=await body_json(request)
    try: wid=int(data.get("id"))
    except Exception: raise web.HTTPBadRequest(text="Invalid ID")
    action=data.get("action")
    async with core.db_pool.acquire() as c:
        async with c.transaction():
            if action=="approve":
                r=await c.fetchrow("UPDATE advertiser_withdrawals SET status='approved' WHERE withdrawal_id=$1 AND status='pending' RETURNING user_id",wid)
                if r: await c.execute("UPDATE advertiser_transactions SET status='approved' WHERE note=$1 AND status='pending'",f"Withdrawal request #{wid}")
            elif action=="reject":
                r=await c.fetchrow("UPDATE advertiser_withdrawals SET status='rejected' WHERE withdrawal_id=$1 AND status='pending' RETURNING user_id,amount",wid)
                if r:
                    await c.execute("UPDATE advertiser_wallets SET available=available+$1,updated_at=NOW() WHERE user_id=$2",r["amount"],r["user_id"])
                    await c.execute("UPDATE advertiser_transactions SET status='rejected' WHERE note=$1",f"Withdrawal request #{wid}")
            elif action=="paid":
                txid=str(data.get("txid","")).strip()
                if not core.valid_txid(txid): raise web.HTTPBadRequest(text="Invalid 64-character TXID")
                txid=txid.lower().removeprefix("0x")
                r=await c.fetchrow("UPDATE advertiser_withdrawals SET status='paid',txid=$1 WHERE withdrawal_id=$2 AND status='approved' RETURNING user_id",txid,wid)
                if r: await c.execute("UPDATE advertiser_transactions SET status='paid' WHERE note=$1",f"Withdrawal request #{wid}")
            else: raise web.HTTPBadRequest(text="Unknown action")
            if not r: raise web.HTTPNotFound(text="Request not found or already processed")
    return json_response({"ok":True,"message":"Advertiser withdrawal updated."})


async def admin_add_task(request):
    await require_admin(request); data=await body_json(request)
    try: reward=int(data.get("reward")); title=str(data.get("title","")).strip()
    except Exception: raise web.HTTPBadRequest(text="Invalid task")
    if reward<=0 or not title or len(title)>250: raise web.HTTPBadRequest(text="Enter a valid title and reward")
    async with core.db_pool.acquire() as c: tid=await c.fetchval("INSERT INTO tasks(title,reward) VALUES($1,$2) RETURNING task_id",title,reward)
    return json_response({"ok":True,"id":tid})


async def admin_task_action(request):
    await require_admin(request); data=await body_json(request)
    try: tid=int(data.get("id"))
    except Exception: raise web.HTTPBadRequest(text="Invalid task ID")
    async with core.db_pool.acquire() as c: r=await c.fetchrow("UPDATE tasks SET active=FALSE WHERE task_id=$1 RETURNING task_id",tid)
    if not r: raise web.HTTPNotFound(text="Task not found")
    return json_response({"ok":True})


async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")


async def create_web_app():
    app=web.Application(middlewares=[auth_middleware], client_max_size=1024*1024)
    app.router.add_get("/", index)
    app.router.add_get("/app", index)
    app.router.add_get("/app/", index)
    app.router.add_static("/assets/", WEB_DIR, show_index=False)
    app.router.add_get("/health", lambda r: web.Response(text="EarnWave Mini App is running"))
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/tasks", api_tasks)
    app.router.add_post("/api/tasks/submit", api_submit_task)
    app.router.add_get("/api/campaigns", api_campaigns)
    app.router.add_post("/api/campaigns/{cid}/join", api_join_campaign)
    app.router.add_post("/api/campaigns/{cid}/verify", api_verify_campaign)
    app.router.add_get("/api/wallet", api_wallet)
    app.router.add_get("/api/history", api_history)
    app.router.add_post("/api/withdraw", api_withdraw)
    app.router.add_post("/api/advwithdraw", api_advwithdraw)
    app.router.add_post("/api/deposit-proof", api_deposit_proof)
    app.router.add_get("/api/admin/summary", admin_summary)
    app.router.add_get("/api/admin/{kind}", admin_list)
    app.router.add_post("/api/admin/action/{action}", admin_action)
    app.router.add_post("/api/admin/deposit-action", admin_deposit_action)
    app.router.add_post("/api/admin/advwithdraw-action", admin_advwithdraw_action)
    app.router.add_post("/api/admin/tasks", admin_add_task)
    app.router.add_post("/api/admin/tasks/deactivate", admin_task_action)
    return app



async def main():
    await core.setup_database()

    if MINI_APP_URL:
        core.keyboard = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(
                    text="🚀 Open EarnWave",
                    web_app=WebAppInfo(url=MINI_APP_URL)
                )
            ]],
            resize_keyboard=True,
            is_persistent=True,
        )

        await core.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Open EarnWave",
                web_app=WebAppInfo(url=MINI_APP_URL)
            )
        )

    runner = web.AppRunner(await create_web_app())
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()

    print(f"EarnWave Mini App listening on port {port}")

    try:
        await asyncio.Event().wait()
    finally:
        await core.bot.session.close()
        await core.db_pool.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
