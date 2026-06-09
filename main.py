from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random
import re
import time
from pathlib import Path

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))


def _clean_env(val: str | None) -> str:
    if not val:
        return ""
    v = val.strip().strip('"').strip("'")
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v


_ROOT = Path(__file__).resolve().parent
for _f in (".env", "env"):
    if (_ROOT / _f).exists():
        load_dotenv(_ROOT / _f)
        break
else:
    load_dotenv()

TOKEN = _clean_env(os.getenv("DISCORD_TOKEN"))
BANK_NUMBER = _clean_env(os.getenv("BANK_NUMBER"))
BANK_NAME = (_clean_env(os.getenv("BANK_NAME", "tpbank")) or "tpbank").lower()
if BANK_NAME in ("msbbank",):
    BANK_NAME = "msb"
if BANK_NAME in ("tpbank", "tp bank"):
    BANK_NAME = "tpbank"
ACCOUNT_NAME = _clean_env(os.getenv("ACCOUNT_NAME", "NGO DUC DUY"))
BANK_DISPLAY = _clean_env(os.getenv("BANK_DISPLAY", "TP BANK"))
SEPAY_TOKEN = _clean_env(os.getenv("SEPAY_TOKEN") or os.getenv("SEPAY_API_KEY"))
ORDER_EXPIRE_SEC = int(os.getenv("ORDER_EXPIRE_MINUTES", "15")) * 60
PUBLIC_URL = _clean_env(
    os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8080"
).rstrip("/")
WEBHOOK_PORT = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT") or "8080")
SHOP_THUMBNAIL = _clean_env(os.getenv("SHOP_THUMBNAIL", ""))
SUPPORT_TEXT = _clean_env(os.getenv("SUPPORT_TEXT", "Ticket server · DM admin"))
DEPOSIT_MSG_TTL = int(os.getenv("DEPOSIT_MSG_TTL", "120"))
MIN_DEPOSIT = int(os.getenv("MIN_DEPOSIT", "5000"))
USE_UNIQUE_AMOUNT = os.getenv("DEPOSIT_UNIQUE_SUFFIX", "1").lower() not in ("0", "false", "no")

API_AIMBOT_BASE = _clean_env(os.getenv("API_AIMBOT_BASE", "https://aovduy.onrender.com")).rstrip("/")
API_ADMIN_USER = _clean_env(os.getenv("API_ADMIN_USER"))
API_ADMIN_PASS = _clean_env(os.getenv("API_ADMIN_PASS"))

C_NEXUS = 0xF5C451
C_PANEL = 0x1B1F2A
C_AIMBOT = 0xFF4FD8
C_SHOP = 0xD4A017

PRODUCTS = {
    "aimlock_pro": {
        "label": "Aimlock Pro",
        "emoji": "😈",
        "tagline": "Ghim Đầu Cực Mạnh · Hỗ Trợ Đầy Đủ",
        "server": "Aimlock Pro Exe",
        "accent": C_AIMBOT,
        "packages": [
            {"id": "ap_1m", "name": "Aimlock Pro 1 Tháng", "price": 250_000, "duration": "30 ngày", "days": 30},
        ],
    },
}

PKG: dict[str, dict] = {}
for _pk, _pv in PRODUCTS.items():
    for _p in _pv["packages"]:
        PKG[_p["id"]] = {**_p, "product_key": _pk, "product_label": _pv["label"]}


class _VNFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, tz=VN_TZ)
        return dt.strftime(datefmt or "%d/%m/%Y %H:%M:%S")


_handler = logging.StreamHandler()
_handler.setFormatter(_VNFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("shop")

# ─────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────

DATA_FILE = _ROOT / "data.json"
API_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=20)
_api_lock = asyncio.Lock()

balances: dict[int, int] = {}
orders: dict[str, dict] = {}
processed_txns: set[str] = set()
_sepay_auth_failed = False


def _vn_now_str() -> str:
    return datetime.datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _load_data() -> None:
    global balances, orders, processed_txns
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        balances = {int(k): v for k, v in d.get("balances", {}).items()}
        orders = d.get("orders", {})
        processed_txns = set(str(x) for x in d.get("processed_txns", []))
        pending = sum(1 for o in orders.values() if not o.get("paid"))
        log.info("Da tai data: %d don (%d cho), %d user", len(orders), pending, len(balances))
    except FileNotFoundError:
        log.info("Chua co data.json — tao moi")
    except Exception as e:
        log.error("Loi doc data: %s", e)


def _save_data() -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "balances": {str(k): v for k, v in balances.items()},
                    "orders": orders,
                    "processed_txns": sorted(processed_txns)[-5000:],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        log.error("Loi ghi data: %s", e)


_load_data()


def _fmt(n: int) -> str:
    return f"{n:,}₫"


def get_balance(uid: int) -> int:
    return balances.get(uid, 0)


def add_balance(uid: int, amount: int) -> int:
    balances[uid] = balances.get(uid, 0) + amount
    _save_data()
    return balances[uid]


def deduct_balance(uid: int, amount: int) -> bool:
    if balances.get(uid, 0) < amount:
        return False
    balances[uid] -= amount
    _save_data()
    return True


# ─────────────────────────────────────────────────────────────
# API KEY (login + createkey)
# ─────────────────────────────────────────────────────────────


def _api_base(product_key: str) -> str:
    return API_AIMBOT_BASE


def _duration_payload(pkg: dict) -> dict:
    if pkg.get("hours"):
        return {"duration_hours": int(pkg["hours"])}
    return {"days": int(pkg.get("days") or 1)}


def _extract_key(data: dict) -> str | None:
    if not isinstance(data, dict):
        return None
    k = data.get("key") or data.get("key_string") or data.get("license")
    if isinstance(k, dict):
        k = k.get("key")
    return str(k).strip() if k else None


async def api_create_key(product_key: str, pkg: dict, buyer_id: int) -> str | None:
    """Dang nhap admin + POST /api/createkey — tra ve key string."""
    if not API_ADMIN_USER or not API_ADMIN_PASS:
        log.error("Thieu API_ADMIN_USER / API_ADMIN_PASS tren Render")
        return None

    base = _api_base(product_key)
    note = f"discord-{pkg['id']}-u{buyer_id}"
    body = {
        **_duration_payload(pkg),
        "key_type": "single_device",
        "created_by": "DucDuyBoutique",
        "note": note,
    }

    async with _api_lock:
        try:
            async with aiohttp.ClientSession(
                timeout=API_TIMEOUT,
                headers={"User-Agent": "DucDuyBoutique/3.0", "Accept": "application/json"},
            ) as session:
                login = await session.post(
                    f"{base}/api/login",
                    json={"username": API_ADMIN_USER, "password": API_ADMIN_PASS},
                )
                if login.status != 200:
                    txt = await login.text()
                    log.error("API login %s fail %s: %s", product_key, login.status, txt[:200])
                    return None

                log.info("API login OK @ %s", base)
                resp = await session.post(f"{base}/api/createkey", json=body)
                raw = await resp.text()
                try:
                    data = json.loads(raw) if raw.strip().startswith("{") else {}
                except json.JSONDecodeError:
                    data = {}

                if resp.status not in (200, 201):
                    log.error("API createkey %s %s: %s", product_key, resp.status, raw[:250])
                    return None

                key = _extract_key(data)
                if key:
                    log.info("API key OK [%s] %s…", pkg["id"], key[:12])
                return key
        except asyncio.TimeoutError:
            log.error("API timeout %s — server co the dang ngu (Render free)", base)
            return None
        except Exception as e:
            log.error("API loi %s: %s", product_key, e)
            return None


def api_nodes_text() -> str:
    lines = []
    for pk, pv in PRODUCTS.items():
        ok = "🟢" if API_ADMIN_USER and API_ADMIN_PASS else "🔴"
        lines.append(f"{ok} {pv['emoji']} **{pv['label']}** · `{_api_base(pk)}`")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# DON NAP + QR
# ─────────────────────────────────────────────────────────────


def _make_order_id() -> str:
    base = "NAP" + str(int(time.time()))
    oid, n = base, 0
    while oid in orders:
        n += 1
        oid = base + str(n)
    return oid


def _pending_transfer_amounts() -> set[int]:
    return {
        int(o.get("transfer_amount") or o.get("amount") or 0)
        for o in orders.values()
        if not o.get("paid") and not _order_expired(o)
    }


def _alloc_transfer_amount(base: int) -> int:
    if not USE_UNIQUE_AMOUNT:
        return base
    used = _pending_transfer_amounts()
    for off in range(1, 1000):
        t = base + off
        if t not in used:
            return t
    return base + random.randint(1000, 9999)


def _order_expired(o: dict) -> bool:
    return (time.time() - float(o.get("created_at", 0))) > ORDER_EXPIRE_SEC


def _credit(o: dict) -> int:
    return int(o.get("base_amount") or o.get("amount") or 0)


def _transfer(o: dict) -> int:
    return int(o.get("transfer_amount") or o.get("amount") or 0)


def create_order(user_id: int, base: int, sepay_since_id: int) -> tuple[str, int, int]:
    oid = _make_order_id()
    transfer = _alloc_transfer_amount(base)
    orders[oid] = {
        "user_id": user_id,
        "base_amount": base,
        "transfer_amount": transfer,
        "paid": False,
        "created_at": time.time(),
        "created_at_vn": _vn_now_str(),
        "sepay_since_id": sepay_since_id,
    }
    _save_data()
    return oid, base, transfer


def qr_url(transfer: int, oid: str) -> str:
    name = ACCOUNT_NAME.replace(" ", "%20")
    return (
        f"https://img.vietqr.io/image/{BANK_NAME}-{BANK_NUMBER}-compact2.png"
        f"?amount={transfer}&addInfo={oid}&accountName={name}"
    )


def deposit_embed(base: int, transfer: int, oid: str) -> discord.Embed:
    e = discord.Embed(
        title="💳 Thông tin nạp tiền",
        description=f"Chuyển **đúng** số tiền và nội dung bên dưới. Bot cộng **`{base:,}` VNĐ** vào ví.",
        color=C_NEXUS,
    )
    e.add_field(
        name="💵 Chi tiết",
        value=(
            f"**Số tiền PHẢI chuyển:** `{transfer:,}` VNĐ\n"
            f"**Cộng vào ví:** `{base:,}` VNĐ\n"
            f"**Nội dung CK:** `{oid}`"
        ),
        inline=False,
    )
    e.add_field(
        name="🏛 Tài khoản",
        value=f"**{ACCOUNT_NAME}** · {BANK_DISPLAY}\n`{BANK_NUMBER}`",
        inline=False,
    )
    e.add_field(
        name="💳 Thanh toán",
        value="**Tự Động** · **Nhanh Chóng** · **Uy Tín**",
        inline=False,
    )
    e.add_field(
        name="💡 Lưu ý",
        value=(
            "1. Tạo đơn xong **rồi mới chuyển**\n"
            f"2. Đúng `{transfer:,}` VNĐ (không làm tròn)\n"
            f"3. Ghi nội dung `{oid}`\n"
            "4. Tự cộng trong ~1–2 phút"
        ),
        inline=False,
    )
    e.set_image(url=qr_url(transfer, oid))
    e.set_footer(text=f"Hết hạn {ORDER_EXPIRE_SEC // 60} phút · {oid}")
    return e


# ─────────────────────────────────────────────────────────────
# SEPAY
# ─────────────────────────────────────────────────────────────

SEPAY_URL = "https://my.sepay.vn/userapi/transactions/list"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)


def _parse_amount(val) -> int:
    try:
        return int(float(val or 0))
    except (TypeError, ValueError):
        return 0


def _txn_amount(txn: dict) -> int:
    for k in ("amount_in", "transferAmount", "amount"):
        v = txn.get(k)
        if v is not None and str(v).strip():
            n = _parse_amount(v)
            if n > 0:
                return n
    return 0


def _txn_text(txn: dict) -> str:
    parts = [
        txn.get("transaction_content"),
        txn.get("content"),
        txn.get("description"),
        txn.get("code"),
        txn.get("reference_number"),
        txn.get("referenceCode"),
    ]
    return " ".join(str(p or "") for p in parts).upper()


def _txn_date(txn: dict) -> str:
    return str(txn.get("transaction_date") or txn.get("transactionDate") or "").strip()


def _txn_ts(txn: dict) -> float | None:
    s = _txn_date(txn)[:19]
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=VN_TZ).timestamp()
    except ValueError:
        return None


def _txn_id(txn: dict) -> int:
    try:
        return int(str(txn.get("id") or "0"))
    except ValueError:
        return 0


def _txn_fp(txn: dict) -> str:
    tid = str(txn.get("id") or "").strip()
    if tid and tid not in ("0", "None"):
        return "id:" + tid
    ref = str(txn.get("reference_number") or txn.get("referenceCode") or "").strip()
    if ref:
        return "ref:" + ref
    return f"fp:{_txn_date(txn)}|{_txn_amount(txn)}|{_txn_text(txn)[:60]}"


def _is_incoming(txn: dict) -> bool:
    t = str(txn.get("transferType") or "").lower()
    if t == "out":
        return False
    if t == "in":
        return True
    try:
        ain = float(txn.get("amount_in") or 0)
        aout = float(txn.get("amount_out") or 0)
        if aout > 0 and ain <= 0:
            return False
        return ain > 0
    except (TypeError, ValueError):
        return _txn_amount(txn) > 0


def _nap_in_text(oid: str, text: str) -> bool:
    if not text:
        return False
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    up = oid.upper()
    if up in compact:
        return True
    return any(m == up for m in re.findall(r"NAP\d{8,}", compact))


def _txn_ok_for_order(txn: dict, order: dict) -> bool:
    """GD moi (ID > moc luc tao don) va sau thoi diem tao don."""
    tid = _txn_id(txn)
    since = int(order.get("sepay_since_id") or 0)
    if tid and since and tid <= since:
        return False
    created = float(order.get("created_at") or 0)
    ts = _txn_ts(txn)
    if ts is None:
        return bool(tid and since and tid > since)
    if ts < created + 2:
        return False
    if ts > created + ORDER_EXPIRE_SEC + 300:
        return False
    return True


def _find_order(txn: dict) -> tuple[str | None, str | None]:
    fp = _txn_fp(txn)
    if fp in processed_txns:
        return None, None
    if not _is_incoming(txn):
        return None, None
    amt = _txn_amount(txn)
    if amt <= 0:
        return None, None

    text = _txn_text(txn)
    pending = [(oid, o) for oid, o in orders.items() if not o.get("paid") and not _order_expired(o)]

    # 1) Ma NAP trong noi dung
    for oid, o in sorted(pending, key=lambda x: x[1]["created_at"], reverse=True):
        if not _nap_in_text(oid, text):
            continue
        if not _txn_ok_for_order(txn, o):
            continue
        if amt >= _transfer(o):
            log.info("Khop NAP %s | +%d | %s", oid, _credit(o), text[:50])
            return oid, fp

    # 2) Dung so tien CK
    for oid, o in sorted(pending, key=lambda x: x[1]["created_at"], reverse=True):
        if amt != _transfer(o):
            continue
        if not _txn_ok_for_order(txn, o):
            continue
        log.info("Khop CK %s | %d | +%d", oid, amt, _credit(o))
        return oid, fp

    return None, None


async def sepay_fetch(limit: int = 50) -> tuple[int, list[dict]]:
    global _sepay_auth_failed
    if not SEPAY_TOKEN or _sepay_auth_failed:
        return (401 if _sepay_auth_failed else 0), []
    headers = {"Authorization": f"Bearer {SEPAY_TOKEN}", "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(SEPAY_URL, headers=headers, params={"limit": limit}, timeout=HTTP_TIMEOUT) as r:
                body = await r.text()
                if r.status == 401:
                    _sepay_auth_failed = True
                    log.error("SePay 401 — cap nhat SEPAY_TOKEN tren Render")
                    return 401, []
                if r.status != 200:
                    log.warning("SePay HTTP %s: %s", r.status, body[:150])
                    return r.status, []
                data = json.loads(body) if body.strip().startswith("{") else {}
                txns = data.get("transactions") or []
                if BANK_NUMBER:
                    want = re.sub(r"\D", "", BANK_NUMBER)

                    def _bank_ok(t: dict) -> bool:
                        acct = re.sub(r"\D", "", str(t.get("account_number") or ""))
                        if not acct:
                            return True
                        return acct == want or acct.endswith(want) or want.endswith(acct)

                    txns = [t for t in txns if _bank_ok(t)]
                return 200, txns
    except Exception as e:
        log.error("SePay loi: %s", e)
        return 0, []


async def sepay_latest_id() -> int:
    st, txns = await sepay_fetch(10)
    if st != 200 or not txns:
        return 0
    return max(_txn_id(t) for t in txns)


async def lock_old_txns() -> None:
    """Khoa GD cu tren SePay — khong dung lai cho don moi."""
    st, txns = await sepay_fetch(100)
    if st != 200:
        return
    added = 0
    for t in txns:
        fp = _txn_fp(t)
        if fp not in processed_txns:
            processed_txns.add(fp)
            added += 1
    if added:
        _save_data()
        log.info("Da khoa %d GD SePay cu (chi nhan CK sau khi tao don moi)", added)


# ─────────────────────────────────────────────────────────────
# XAC NHAN THANH TOAN
# ─────────────────────────────────────────────────────────────


async def confirm_payment(oid: str, fp: str | None = None) -> None:
    o = orders.get(oid)
    if not o or o.get("paid"):
        return
    if fp and fp in processed_txns:
        return

    o["paid"] = True
    o["paid_at"] = time.time()
    if fp:
        processed_txns.add(fp)
    _save_data()

    uid = int(o["user_id"])
    credit = _credit(o)
    bal = add_balance(uid, credit)
    log.info("XAC NHAN %s | +%s | user %s | du %s", oid, credit, uid, bal)

    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        em = discord.Embed(
            title="✅ Nạp tiền thành công",
            description=f"Đã cộng **`{credit:,}` VNĐ** vào ví.\nSố dư: **`{bal:,}` VNĐ**",
            color=0x2ECC71,
        )
        em.add_field(name="Mã đơn", value=f"`{oid}`")
        await user.send(embed=em)
    except Exception as e:
        log.warning("Khong DM user %s: %s", uid, e)

    eph = o.get("ephemeral")
    if eph:
        try:
            wh = discord.Webhook.partial(int(eph["application_id"]), eph["token"], client=bot)
            ok = discord.Embed(
                title="✅ Nạp thành công",
                description=f"Đã cộng `{credit:,}` VNĐ · Số dư `{bal:,}` VNĐ\nCheck DM để xem chi tiết.",
                color=0x2ECC71,
            )
            await wh.edit_message(int(eph["message_id"]), embed=ok, attachments=[])
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# WEBHOOK HTTP
# ─────────────────────────────────────────────────────────────


def _unwrap(body) -> dict:
    if not isinstance(body, dict):
        return {}
    for k in ("transaction", "data", "payload"):
        inner = body.get(k)
        if isinstance(inner, dict):
            return inner
    return body


async def _parse_req(request: web.Request) -> dict:
    ct = (request.headers.get("Content-Type") or "").lower()
    if "json" in ct:
        raw = await request.json()
        return _unwrap(raw) if isinstance(raw, dict) else {}
    if "form" in ct:
        post = await request.post()
        return _unwrap({k: (v[0] if isinstance(v, (list, tuple)) else v) for k, v in post.items()})
    text = await request.text()
    if text.strip().startswith("{"):
        return _unwrap(json.loads(text))
    return {}


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "ducduy-boutique", "time_vn": _vn_now_str()})


async def sepay_webhook(request: web.Request) -> web.Response:
    try:
        txn = _unwrap(await _parse_req(request))
        oid, fp = _find_order(txn)
        if oid:
            await confirm_payment(oid, fp)
        return web.json_response({"success": True})
    except Exception as e:
        log.error("Webhook loi: %s", e)
        return web.json_response({"success": False}, status=500)


async def start_http() -> None:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/webhook", sepay_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT).start()
    log.info("Webhook %s/webhook (port %s)", PUBLIC_URL, WEBHOOK_PORT)


# ─────────────────────────────────────────────────────────────
# POLL SEPAY
# ─────────────────────────────────────────────────────────────


@tasks.loop(seconds=10)
async def poll_sepay() -> None:
    pending = [oid for oid, o in orders.items() if not o.get("paid") and not _order_expired(o)]
    if not pending or not SEPAY_TOKEN or _sepay_auth_failed:
        return
    st, txns = await sepay_fetch(60)
    if st != 200:
        return
    for txn in txns:
        oid, fp = _find_order(txn)
        if oid:
            await confirm_payment(oid, fp)


@poll_sepay.before_loop
async def _poll_wait() -> None:
    await bot.wait_until_ready()


# ─────────────────────────────────────────────────────────────
# DISCORD UI
# ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def hub_embed() -> discord.Embed:
    e = discord.Embed(
        title="🛒 ShopClone - Auto Buy",
        description=(
            "**🔥 Danh mục đang bán**\n"
            "===============================================\n"
            "➡️ 😈 **Aimlock Pro**\n"
            "💰 **Giá:** `250,000 VNĐ`\n"
            "📦 **Kho Còn:** `Sẵn Hàng` (Auto Key)\n\n"
            "**📞 Support**\n"
            f"✉️ **Tạo Ticket:** {SUPPORT_TEXT}\n"
            f"👤 **Hỗ trợ:** @admin (hoặc direct message)\n\n"
            "Vui lòng chọn danh mục bên dưới để tiếp tục"
        ),
        color=C_SHOP,
    )
    if SHOP_THUMBNAIL:
        e.set_thumbnail(url=SHOP_THUMBNAIL)
    elif bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    return e


def guide_embed() -> discord.Embed:
    e = discord.Embed(
        title="📖  Hướng dẫn sử dụng",
        description=(
            "**Bước 1 — Nạp tiền**\n"
            "Bấm `Nạp tiền` → nhập số VNĐ → quét QR → chuyển **đúng số CK** + mã `NAP...`\n\n"
            "**Bước 2 — Mua license**\n"
            "Menu `Chọn danh mục...` → Chọn Aimlock Pro → chọn gói → xác nhận\n\n"
            "**Bước 3 — Nhận key**\n"
            "Mở DM với bot — key gửi tự động trong vài giây\n\n"
            "══════════════════════\n"
            "⚠️ Chuyển **sau** khi tạo đơn nạp\n"
            "⚠️ Không làm tròn số tiền (vd. 50.001đ)\n"
            "⚠️ Bật tin nhắn riêng từ thành viên server"
        ),
        color=C_PANEL,
    )
    e.set_footer(text="DUCDUY BOUTIQUE · Hỗ trợ: " + SUPPORT_TEXT[:40])
    return e


def vault_embed(pk: str) -> discord.Embed:
    pv = PRODUCTS[pk]
    lines = []
    for i, p in enumerate(pv["packages"], 1):
        lines.append(f"`{i}.` ⏳ **{p['duration']}** ─ **{p['price']:,} VNĐ**")
    e = discord.Embed(
        title=f"{pv['emoji']}  {pv['label']} — Bảng giá",
        description=(
            f"_{pv['tagline']}_\n"
            f"☁️ **{pv['server']}**\n"
            "══════════════════\n"
            + "\n".join(lines)
            + "\n══════════════════"
        ),
        color=pv["accent"],
    )
    e.set_footer(text="Chọn gói tại menu · Trừ tiền ví boutique")
    return e


def license_dm_embed(pv: dict, pkg: dict, keys: list[str]) -> discord.Embed:
    block = "\n".join(f"▸ `{k}`" for k in keys)
    e = discord.Embed(title=f"◈ LICENSE · {pv['label'].upper()}", color=pv["accent"])
    e.description = (
        "```fix\n"
        "┏━━━━━━━━ LICENSE UNLOCKED ━━━━━━━━┓\n"
        f"┃  {pv['emoji']}  {pkg['name']}\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        "```\n"
        f"{block}\n\n"
        f"⏱️ **Hạn:** {pkg['duration']}\n"
        f"☁️ **{pv['server']}**\n\n"
        "⚠️ Không chia sẻ key · một thiết bị"
    )
    e.set_footer(text="ducduy boutique · auto api")
    return e


class LaneSelect(discord.ui.Select):
    """Menu chọn lane — layout tương tự shop panel nhưng nội dung boutique."""

    def __init__(self):
        super().__init__(
            placeholder="📌  Chọn danh mục...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Aimlock Pro",
                    value="aimlock_pro",
                    emoji="😈",
                    description="Aimlock Pro · Ghim đầu · 250.000đ",
                ),
            ],
            custom_id="boutique_lane_select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        pk = self.values[0]
        await interaction.response.send_message(
            embed=vault_embed(pk),
            view=VaultView(pk),
            ephemeral=True,
        )


class PackageSelect(discord.ui.Select):
    def __init__(self, pk: str):
        pv = PRODUCTS[pk]
        super().__init__(
            placeholder="📦  Chọn gói thời gian...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"{p['duration']}  ·  {p['price']:,}đ",
                    value=p["id"],
                    description=p["name"][:100],
                    emoji="⚡",
                )
                for p in pv["packages"]
            ],
            row=0,
        )
        self.pk = pk

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BuyModal(self.values[0]))


class VaultView(discord.ui.View):
    def __init__(self, pk: str):
        super().__init__(timeout=300)
        self.add_item(PackageSelect(pk))


class BoutiquePanelView(discord.ui.View):
    """Panel chính: dropdown lane + 3 nút (nạp / số dư / HD)."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(LaneSelect())

    @discord.ui.button(
        label="Nạp tiền",
        emoji="💳",
        style=discord.ButtonStyle.success,
        custom_id="boutique_deposit",
        row=1,
    )
    async def deposit(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(DepositModal())

    @discord.ui.button(
        label="Số dư",
        emoji="💰",
        style=discord.ButtonStyle.primary,
        custom_id="boutique_balance",
        row=1,
    )
    async def balance(self, interaction: discord.Interaction, _):
        bal = get_balance(interaction.user.id)
        em = discord.Embed(
            title="💰  Số dư ví Boutique",
            description=f"```fix\n{bal:,} VNĐ\n```",
            color=C_SHOP,
        )
        em.set_footer(text="Nạp thêm tại nút Nạp tiền")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @discord.ui.button(
        label="Hướng dẫn",
        emoji="📖",
        style=discord.ButtonStyle.secondary,
        custom_id="boutique_guide",
        row=1,
    )
    async def guide_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_message(embed=guide_embed(), ephemeral=True)


class DepositModal(discord.ui.Modal, title="💳 Nạp tiền"):
    amount = discord.ui.TextInput(label="Số tiền (VNĐ)", placeholder="50000", min_length=4, max_length=9)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.replace(",", "").replace(".", "").strip()
        try:
            base = int(raw)
        except ValueError:
            return await interaction.response.send_message("❌ Số tiền không hợp lệ.", ephemeral=True)
        if base < MIN_DEPOSIT:
            return await interaction.response.send_message(f"❌ Tối thiểu **{MIN_DEPOSIT:,}** VNĐ.", ephemeral=True)
        if not BANK_NUMBER:
            return await interaction.response.send_message("❌ Chưa cấu hình BANK_NUMBER.", ephemeral=True)

        since = await sepay_latest_id() if SEPAY_TOKEN else 0
        oid, base, transfer = create_order(interaction.user.id, base, since)
        log.info("Tao don %s | CK %s | +%s | user %s | sepay>%s", oid, transfer, base, interaction.user.id, since)

        await interaction.response.send_message(embed=deposit_embed(base, transfer, oid), ephemeral=True)
        try:
            msg = await interaction.original_response()
            orders[oid]["ephemeral"] = {
                "application_id": interaction.application_id,
                "token": interaction.token,
                "message_id": msg.id,
            }
            _save_data()
        except Exception:
            pass


class BuyModal(discord.ui.Modal):
    qty = discord.ui.TextInput(label="Số lượng", default="1", max_length=2)

    def __init__(self, pkg_id: str):
        self.pkg_id = pkg_id
        p = PKG[pkg_id]
        super().__init__(title=f"🛒 {p['name']}")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            q = max(1, int(self.qty.value.strip()))
        except ValueError:
            return await interaction.response.send_message("❌ Số lượng không hợp lệ.", ephemeral=True)

        p = PKG[self.pkg_id]
        total = p["price"] * q
        uid = interaction.user.id
        bal = get_balance(uid)
        if bal < total:
            return await interaction.response.send_message(
                f"❌ Không đủ tiền.\nSố dư: **{bal:,}** · Cần: **{total:,}** · Thiếu: **{total - bal:,}** VNĐ",
                ephemeral=True,
            )
        if not API_ADMIN_USER or not API_ADMIN_PASS:
            return await interaction.response.send_message(
                "❌ Bot chưa cấu hình API admin trên server.", ephemeral=True
            )

        pv = PRODUCTS[p["product_key"]]
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="⏳ Đang tạo license...",
                description=(
                    f"☁️ **{pv['server']}** · {pv['label']}\n"
                    f"Gói **{p['duration']}** × `{q}`\nVui lòng đợi 10–30 giây."
                ),
                color=pv["accent"],
            )
        )

        deduct_balance(uid, total)
        keys: list[str] = []
        for i in range(q):
            k = await api_create_key(p["product_key"], p, uid)
            if k:
                keys.append(k)
            else:
                add_balance(uid, p["price"] * (q - len(keys)))
                break

        new_bal = get_balance(uid)
        paid = p["price"] * len(keys)
        if keys:
            em = discord.Embed(
                title="◈ GIAO DỊCH HOÀN TẤT",
                description=(
                    f"**{p['name']}**\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏱️ `{p['duration']}` · 🔢 `{len(keys)}` key\n"
                    f"💸 −`{_fmt(paid)}` · 💰 ví `{_fmt(new_bal)}`"
                ),
                color=pv["accent"],
            )
        else:
            em = discord.Embed(
                title="❌ Không tạo được key",
                description=(
                    "API server có thể đang ngủ hoặc lỗi.\n"
                    "Đã hoàn tiền vào ví. Thử lại sau 1 phút."
                ),
                color=0xE74C3C,
            )
        await interaction.edit_original_response(embed=em)

        if keys:
            try:
                await interaction.user.send(embed=license_dm_embed(pv, p, keys))
            except discord.Forbidden:
                await interaction.followup.send("⚠️ Bật DM để nhận key.", ephemeral=True)


# ─────────────────────────────────────────────────────────────
# LENH
# ─────────────────────────────────────────────────────────────


@bot.command(name="shop", aliases=["menu", "s"])
async def cmd_shop(ctx: commands.Context):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(embed=hub_embed(), view=BoutiquePanelView())


@bot.command()
@commands.has_permissions(administrator=True)
async def xacnhan(ctx: commands.Context, oid: str):
    oid = oid.upper()
    if oid not in orders:
        return await ctx.send(f"❌ Không có đơn `{oid}`.")
    await confirm_payment(oid)
    await ctx.send(f"✅ Đã xác nhận `{oid}`.")


@bot.command()
@commands.has_permissions(administrator=True)
async def congcoin(ctx: commands.Context, member: discord.Member, amount: int):
    bal = add_balance(member.id, amount)
    await ctx.send(f"✅ Cộng {amount:,} VNĐ cho {member.mention} · Số dư: {bal:,}")


@bot.command()
@commands.has_permissions(administrator=True)
async def trucoin(ctx: commands.Context, member: discord.Member, amount: int):
    if not deduct_balance(member.id, amount):
        return await ctx.send("❌ Số dư không đủ.")
    await ctx.send(f"✅ Trừ {amount:,} VNĐ · Số dư: {get_balance(member.id):,}")


@bot.command()
@commands.has_permissions(administrator=True)
async def doncho(ctx: commands.Context):
    pending = [(oid, o) for oid, o in orders.items() if not o.get("paid") and not _order_expired(o)]
    if not pending:
        return await ctx.send("✅ Không có đơn chờ.")
    lines = [
        f"`{oid}` CK `{_transfer(o):,}` → +`{_credit(o):,}` <@{o['user_id']}>`"
        for oid, o in pending[:15]
    ]
    await ctx.send(embed=discord.Embed(title=f"⏳ Đơn chờ ({len(pending)})", description="\n".join(lines), color=0xFFAA00))


@bot.command()
@commands.has_permissions(administrator=True)
async def info(ctx: commands.Context):
    sepay = "OK" if SEPAY_TOKEN and not _sepay_auth_failed else ("401" if _sepay_auth_failed else "chưa cấu hình")
    pending = sum(1 for o in orders.values() if not o.get("paid"))
    await ctx.send(
        f"**Bot:** {bot.user}\n"
        f"**Webhook:** `{PUBLIC_URL}/webhook`\n"
        f"**SePay:** `{sepay}`\n"
        f"**Đơn chờ:** {pending}\n"
        f"**Giờ VN:** {_vn_now_str()}\n"
        f"**API:**\n{api_nodes_text()}\n"
        f"**AimLock Backend:** `{API_AIMBOT_BASE}`"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def sepaycheck(ctx: commands.Context):
    if not SEPAY_TOKEN:
        return await ctx.send("❌ Chưa có SEPAY_TOKEN.")
    st, txns = await sepay_fetch(8)
    if st == 401:
        return await ctx.send("❌ SePay 401 — đổi token mới trên Render.")
    lines = ["**GD gần nhất:**"]
    for t in txns[:5]:
        lines.append(f"`{_txn_date(t)}` +{_txn_amount(t):,} — `{_txn_text(t)[:35]}`")
    if not txns:
        lines.append("_Không có_")
    await ctx.send("\n".join(lines))


@bot.command()
@commands.has_permissions(administrator=True)
async def sepayreset(ctx: commands.Context):
    global _sepay_auth_failed
    _sepay_auth_failed = False
    await ctx.send("✅ Đã reset SePay. Thử `!sepaycheck`.")


@bot.command()
@commands.has_permissions(administrator=True)
async def testkey(ctx: commands.Context, pkg_id: str = "ap_1m"):
    if pkg_id not in PKG:
        return await ctx.send(f"❌ Gói không tồn tại. VD: `ap_1m`")
    await ctx.send(f"⏳ Đang tạo key `{pkg_id}`...", delete_after=5)
    k = await api_create_key(PKG[pkg_id]["product_key"], PKG[pkg_id], ctx.author.id)
    if k:
        await ctx.send(f"✅ Key: `{k}`", delete_after=30)
    else:
        await ctx.send("❌ Lỗi API — xem log Render", delete_after=15)


# ─────────────────────────────────────────────────────────────
# KHOI DONG
# ─────────────────────────────────────────────────────────────

_http_started = False


@bot.event
async def on_ready():
    global _http_started
    log.info("Online: %s | VN %s", bot.user, _vn_now_str())
    bot.add_view(BoutiquePanelView())
    if not _http_started:
        await start_http()
        _http_started = True
    if not poll_sepay.is_running():
        poll_sepay.start()
    if SEPAY_TOKEN and not _sepay_auth_failed:
        await lock_old_txns()
    if not SEPAY_TOKEN:
        log.warning("Thieu SEPAY_TOKEN — khong tu cong tien")
    if not API_ADMIN_USER or not API_ADMIN_PASS:
        log.warning("Thieu API_ADMIN_USER/PASS — khong tao duoc key!")
    else:
        log.info("API AimLock Backend: %s", API_AIMBOT_BASE)


if not TOKEN:
    raise SystemExit("Thieu DISCORD_TOKEN trong env")

bot.run(TOKEN)
