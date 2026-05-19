import discord
from discord.ext import commands, tasks
from aiohttp import web
import aiohttp
import json
import os
import random
import time
import logging
from pathlib import Path

from dotenv import load_dotenv

# Load env file (supports both "env" and ".env")
for name in (".env", "env"):
    p = Path(__file__).resolve().parent / name
    if p.exists():
        load_dotenv(p)
        break

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
SEPAY_TOKEN = os.getenv("SEPAY_TOKEN")

BANK_NAME = os.getenv("BANK_NAME", "msb")
BANK_NUMBER = os.getenv("BANK_NUMBER")

PORT = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT") or "8080")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("PAYMENT")

# =========================
# BOT
# =========================

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# DATABASE
# =========================

DATA_FILE = "data.json"

balances = {}
orders = {}
used_txns = set()

def save():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "balances": balances,
            "orders": orders
        }, f, indent=2)

def load():
    global balances, orders
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
            balances = {int(k): v for k, v in d.get("balances", {}).items()}
            orders = d.get("orders", {})

load()

# =========================
# BALANCE
# =========================

def add_balance(uid, amount):
    balances[uid] = balances.get(uid, 0) + amount
    save()
    return balances[uid]

def get_balance(uid):
    return balances.get(uid, 0)

# =========================
# ORDER CREATE (PRO FIX)
# =========================

def create_order(amount: int):
    """
    PRO SYSTEM:
    - amount unique để match 100%
    """
    oid = "NAP" + str(random.randint(10000, 99999))
    unique_amount = amount + random.randint(1, 999)

    orders[oid] = {
        "user_id": None,
        "amount": unique_amount,
        "base_amount": amount,
        "paid": False,
        "txn_id": None,
        "created": time.time()
    }

    save()
    return oid, unique_amount

# =========================
# SEPAY PARSER PRO
# =========================

def get_amount(txn):
    """Webhook dùng transferAmount; API list dùng amount_in (string)."""
    for k in ("transferAmount", "amount_in", "amount", "value"):
        try:
            v = txn.get(k)
            if v is not None and str(v).strip() not in ("", "0", "0.00"):
                return int(float(v))
        except (TypeError, ValueError):
            pass
    return 0

def is_incoming(txn):
    t = txn.get("transferType")
    if t is not None:
        return str(t).lower() == "in"
    try:
        return float(txn.get("amount_in") or 0) > 0
    except (TypeError, ValueError):
        return False

def match_txn(txn, oid, order):
    txn_id = str(txn.get("id") or "")

    if not txn_id or txn_id == "None":
        return False

    if txn_id == order.get("txn_id"):
        return False

    if not is_incoming(txn):
        return False

    amount = get_amount(txn)
    content = str(txn.get("content") or txn.get("transaction_content") or "")

    log.info("[CHECK] %s | amount=%s need=%s | content=%s", oid, amount, order["amount"], content[:80])

    if amount != order["amount"]:
        return False

    content_up = content.upper()
    if content_up:
        if oid.upper() in content_up:
            pass
        elif any(k.upper() in content_up for k in orders if k != oid):
            return False

    order["txn_id"] = txn_id
    return True

# =========================
# CONFIRM PAYMENT
# =========================

async def confirm_payment(order_id):
    order = orders.get(order_id)

    if not order or order["paid"]:
        return

    uid = order.get("user_id")
    if not uid:
        log.error("[CONFIRM] %s missing user_id", order_id)
        return

    order["paid"] = True
    amount = order["base_amount"]

    new_balance = add_balance(uid, amount)
    log.info("[PAID] %s user=%s +%s balance=%s", order_id, uid, amount, new_balance)

    save()

    try:
        user = await bot.fetch_user(uid)

        embed = discord.Embed(
            title="✅ NẠP TIỀN THÀNH CÔNG",
            color=0x00ff88
        )

        embed.add_field(name="Cộng", value=f"{amount:,}đ", inline=False)
        embed.add_field(name="Số dư", value=f"{new_balance:,}đ", inline=False)

        await user.send(embed=embed)

    except Exception as e:
        log.error("DM FAIL: %s", e)

# =========================
# SEPAY POLLING (BACKUP AUTO)
# =========================

@tasks.loop(seconds=10)
async def poll_sepay():
    if not SEPAY_TOKEN:
        return

    headers = {"Authorization": f"Bearer {SEPAY_TOKEN}"}
    params = {"limit": 200}
    if BANK_NUMBER:
        params["account_number"] = BANK_NUMBER

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://my.sepay.vn/userapi/transactions/list",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    log.error("[POLL] HTTP %s: %s", r.status, await r.text())
                    return
                data = await r.json()
    except Exception as e:
        log.exception("[POLL] request failed: %s", e)
        return

    if data.get("status") not in (None, 200) and data.get("error"):
        log.error("[POLL] API error: %s", data.get("error"))
        return

    txns = data.get("transactions") or []
    pending = sum(1 for o in orders.values() if not o.get("paid"))
    if pending:
        log.info("[POLL] %s txns, %s pending orders", len(txns), pending)

    for txn in txns:
        tid = str(txn.get("id") or "")
        if not tid or tid in used_txns:
            continue

        for oid, order in list(orders.items()):
            if order.get("paid"):
                continue
            if match_txn(txn, oid, order):
                used_txns.add(tid)
                await confirm_payment(oid)
                break

# =========================
# WEBHOOK (REALTIME)
# =========================

async def webhook(request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    log.info("[WEBHOOK] id=%s amount=%s", body.get("id"), get_amount(body))

    for oid, order in list(orders.items()):
        if order.get("paid"):
            continue
        if match_txn(body, oid, order):
            tid = str(body.get("id") or "")
            if tid:
                used_txns.add(tid)
            await confirm_payment(oid)
            # SePay chỉ coi thành công khi body đúng {"success": true}
            return web.json_response({"success": True})

    return web.json_response({"success": True})

async def health(_request):
    return web.json_response({"ok": True, "service": "payment-bot"})

async def start_webhook():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/webhook", webhook)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info("WEBHOOK RUNNING")

# =========================
# DISCORD COMMANDS
# =========================

@bot.command()
async def nap(ctx, amount: int):

    oid, real_amount = create_order(amount)

    orders[oid]["user_id"] = ctx.author.id

    qr = (
        f"https://img.vietqr.io/image/{BANK_NAME}-{BANK_NUMBER}-compact2.png"
        f"?amount={real_amount}&addInfo={oid}"
    )

    embed = discord.Embed(title="💳 NẠP TIỀN AUTO")

    embed.add_field(name="Số tiền phải chuyển", value=f"{real_amount:,}đ", inline=False)
    embed.add_field(name="Mã đơn", value=oid, inline=False)

    embed.set_image(url=qr)

    await ctx.send(embed=embed)

@bot.command()
async def balance(ctx):
    await ctx.send(f"💰 Số dư: {get_balance(ctx.author.id):,}đ")

# =========================
# READY
# =========================

_webhook_started = False

@bot.event
async def on_ready():
    global _webhook_started
    log.info("BOT READY as %s", bot.user)

    if not TOKEN:
        log.error("DISCORD_TOKEN is missing")
    if not SEPAY_TOKEN:
        log.warning("SEPAY_TOKEN is missing — polling disabled, webhook only")
    if not BANK_NUMBER:
        log.warning("BANK_NUMBER is missing — QR may be invalid")

    if not _webhook_started:
        await start_webhook()
        _webhook_started = True
        log.info("WEBHOOK http://0.0.0.0:%s/webhook", PORT)

    if not poll_sepay.is_running():
        poll_sepay.start()

# =========================
# RUN
# =========================

bot.run(TOKEN)
