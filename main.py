"""
DUCDUY BOUTIQUE — Discord Shop Bot
Nap tien tu dong (SePay) + tao key qua API backend + gui DM
San pham: AimLock Pro

CHANGELOG v4:
- FIX: lock_old_txns() KHONG khoa GD moi nua — chi sync tu don da paid
  (nguyen nhan chinh gay mat tien khi bot restart)
- FIX: _alloc_transfer_amount bat dau tu so goc (offset=0), khong cong them
  -> khach chuyen dung so tien se khop ngay
- FIX: _txn_ok_for_order noi long timestamp len 60s (truoc la 2s)
- FIX: giao dien 4 goi: 3 inline + 1 hang rieng (giong anh mau)
- LOG chi tiet hon de debug
"""

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

TOKEN            = _clean_env(os.getenv("DISCORD_TOKEN"))
BANK_NUMBER      = _clean_env(os.getenv("BANK_NUMBER"))
BANK_NAME        = (_clean_env(os.getenv("BANK_NAME", "tpbank")) or "tpbank").lower()
if BANK_NAME in ("msbbank",):
    BANK_NAME = "msb"
if BANK_NAME in ("tpbank", "tp bank"):
    BANK_NAME = "tpbank"
ACCOUNT_NAME     = _clean_env(os.getenv("ACCOUNT_NAME", "NGO DUC DUY"))
BANK_DISPLAY     = _clean_env(os.getenv("BANK_DISPLAY", "TP BANK"))
SEPAY_TOKEN      = _clean_env(os.getenv("SEPAY_TOKEN") or os.getenv("SEPAY_API_KEY"))
ORDER_EXPIRE_SEC = int(os.getenv("ORDER_EXPIRE_MINUTES", "15")) * 60
PUBLIC_URL       = _clean_env(
    os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8080"
).rstrip("/")
WEBHOOK_PORT     = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT") or "8080")
SHOP_THUMBNAIL   = _clean_env(os.getenv("SHOP_THUMBNAIL", ""))
SUPPORT_TEXT     = _clean_env(os.getenv("SUPPORT_TEXT", "Ticket server · DM admin"))
MIN_DEPOSIT      = int(os.getenv("MIN_DEPOSIT", "5000"))
USE_UNIQUE_AMOUNT = os.getenv("DEPOSIT_UNIQUE_SUFFIX", "1").lower() not in ("0", "false", "no")

API_AIMLOCK_BASE = _clean_env(os.getenv("API_AIMLOCK_BASE", "https://aovduy.onrender.com")).rstrip("/")
API_ADMIN_USER   = _clean_env(os.getenv("API_ADMIN_USER"))
API_ADMIN_PASS   = _clean_env(os.getenv("API_ADMIN_PASS"))

C_NEXUS   = 0xF5C451
C_PANEL   = 0x2C2F33
C_AIMLOCK = 0xFF4FD8
C_SHOP    = 0xD4A017
C_GREEN   = 0x2ECC71

# ── SAN PHAM ─────────────────────────────────────────────────
PRODUCTS = {
    "aimlock_pro": {
        "label":   "AimLock Pro",
        "emoji":   "🎯",
        "tagline": "Khoa dau chat · Khong loi dame · On dinh",
        "server":  "AimLock Pro",
        "accent":  C_AIMLOCK,
        "packages": [
            {"id": "ap_1d", "name": "AimLock Pro 1 Ngay",  "price":  15_000, "duration": "1 ngay",  "days":  1},
            {"id": "ap_7d", "name": "AimLock Pro 7 Ngay",  "price":  60_000, "duration": "7 ngay",  "days":  7},
            {"id": "ap_1m", "name": "AimLock Pro 1 Thang", "price": 150_000, "duration": "1 thang", "days": 30},
            {"id": "ap_ob", "name": "AimLock Pro 1 OB",    "price": 250_000, "duration": "1 OB",    "days": 90},
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
        balances       = {int(k): v for k, v in d.get("balances", {}).items()}
        orders         = d.get("orders", {})
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
                    "balances":       {str(k): v for k, v in balances.items()},
                    "orders":         orders,
                    "processed_txns": sorted(processed_txns)[-5000:],
                },
                f, ensure_ascii=False, indent=2,
            )
    except Exception as e:
        log.error("Loi ghi data: %s", e)


_load_data()


def _fmt(n: int) -> str:
    return f"{n:,}d"


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
# API KEY
# ─────────────────────────────────────────────────────────────

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
    if not API_ADMIN_USER or not API_ADMIN_PASS:
        log.error("Thieu API_ADMIN_USER / API_ADMIN_PASS")
        return None

    base = API_AIMLOCK_BASE
    body = {
        **_duration_payload(pkg),
        "key_type":   "single_device",
        "created_by": "DucDuyBoutique",
        "note":       f"discord-{pkg['id']}-u{buyer_id}",
    }

    async with _api_lock:
        try:
            async with aiohttp.ClientSession(
                timeout=API_TIMEOUT,
                headers={"User-Agent": "DucDuyBoutique/4.0", "Accept": "application/json"},
            ) as session:
                login = await session.post(
                    f"{base}/api/login",
                    json={"username": API_ADMIN_USER, "password": API_ADMIN_PASS},
                )
                if login.status != 200:
                    txt = await login.text()
                    log.error("API login fail %s: %s", login.status, txt[:200])
                    return None

                log.info("API login OK @ %s", base)
                resp = await session.post(f"{base}/api/createkey", json=body)
                raw  = await resp.text()
                try:
                    data = json.loads(raw) if raw.strip().startswith("{") else {}
                except json.JSONDecodeError:
                    data = {}

                if resp.status not in (200, 201):
                    log.error("API createkey %s: %s", resp.status, raw[:250])
                    return None

                key = _extract_key(data)
                if key:
                    log.info("API key OK [%s] %s", pkg["id"], key[:16])
                return key
        except asyncio.TimeoutError:
            log.error("API timeout (Render dang ngu): %s", base)
            return None
        except Exception as e:
            log.error("API loi: %s", e)
            return None


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
    """
    FIX: thu so tien goc truoc (offset=0).
    Chi them suffix neu trung voi don dang cho khac.
    """
    if not USE_UNIQUE_AMOUNT:
        return base
    used = _pending_transfer_amounts()
    if base not in used:
        return base
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
    oid      = _make_order_id()
    transfer = _alloc_transfer_amount(base)
    orders[oid] = {
        "user_id":         user_id,
        "base_amount":     base,
        "transfer_amount": transfer,
        "paid":            False,
        "created_at":      time.time(),
        "created_at_vn":   _vn_now_str(),
        "sepay_since_id":  sepay_since_id,
    }
    _save_data()
    log.info("Tao don %s | CK=%s credit=%s uid=%s since_id=%s",
             oid, transfer, base, user_id, sepay_since_id)
    return oid, base, transfer


def qr_url(transfer: int, oid: str) -> str:
    name = ACCOUNT_NAME.replace(" ", "%20")
    return (
        f"https://img.vietqr.io/image/{BANK_NAME}-{BANK_NUMBER}-compact2.png"
        f"?amount={transfer}&addInfo={oid}&accountName={name}"
    )


def deposit_embed(base: int, transfer: int, oid: str) -> discord.Embed:
    e = discord.Embed(
        title="💳  Thông tin nạp tiền",
        description=(
            f"Chuyển **đúng** số tiền và nội dung bên dưới.\n"
            f"Bot sẽ tự cộng **`{base:,}` VNĐ** vào ví trong ~1–2 phút."
        ),
        color=C_NEXUS,
    )
    e.add_field(
        name="💵 Chi tiết chuyển khoản",
        value=(
            f"**Số tiền PHẢI chuyển:** `{transfer:,}` VNĐ\n"
            f"**Cộng vào ví:** `{base:,}` VNĐ\n"
            f"**Nội dung CK:** `{oid}`"
        ),
        inline=False,
    )
    e.add_field(
        name="🏛️ Tài khoản nhận",
        value=f"**{ACCOUNT_NAME}** · {BANK_DISPLAY}\n`{BANK_NUMBER}`",
        inline=False,
    )
    e.add_field(
        name="⚠️ Lưu ý quan trọng",
        value=(
            "① Tạo đơn xong **mới chuyển khoản**\n"
            f"② Chuyển đúng **`{transfer:,}` VNĐ** (không làm tròn)\n"
            f"③ Nội dung: **`{oid}`**\n"
            "④ Tiền tự cộng trong 1–2 phút"
        ),
        inline=False,
    )
    e.set_image(url=qr_url(transfer, oid))
    e.set_footer(text=f"Đơn hết hạn sau {ORDER_EXPIRE_SEC // 60} phút · Mã: {oid}")
    return e


# ─────────────────────────────────────────────────────────────
# SEPAY
# ─────────────────────────────────────────────────────────────

SEPAY_URL    = "https://my.sepay.vn/userapi/transactions/list"
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
        ain  = float(txn.get("amount_in")  or 0)
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
    """
    FIX: noi long dieu kien thoi gian.
    - Neu co since_id: GD phai co ID > since_id
    - Timestamp: cho phep lech 60s truoc thoi diem tao don
    - Khong nhan GD qua han don + 10 phut buffer
    """
    tid   = _txn_id(txn)
    since = int(order.get("sepay_since_id") or 0)

    if tid and since and tid <= since:
        return False

    created = float(order.get("created_at") or 0)
    ts      = _txn_ts(txn)

    if ts is None:
        return bool(tid and since and tid > since)

    # FIX: noi long tu 2s len 60s de xu ly lech dong ho
    if ts < created - 60:
        return False

    # 10 phut buffer sau khi het han
    if ts > created + ORDER_EXPIRE_SEC + 600:
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

    text    = _txn_text(txn)
    pending = [(oid, o) for oid, o in orders.items()
               if not o.get("paid") and not _order_expired(o)]

    # Uu tien 1: khop ma NAP trong noi dung
    for oid, o in sorted(pending, key=lambda x: x[1]["created_at"], reverse=True):
        if not _nap_in_text(oid, text):
            continue
        if not _txn_ok_for_order(txn, o):
            continue
        if amt >= _transfer(o):
            log.info("Khop NAP[text] %s | CK=%d credit=%d | nd=%s",
                     oid, amt, _credit(o), text[:60])
            return oid, fp

    # Uu tien 2: khop chinh xac so tien CK
    for oid, o in sorted(pending, key=lambda x: x[1]["created_at"], reverse=True):
        if amt != _transfer(o):
            continue
        if not _txn_ok_for_order(txn, o):
            continue
        log.info("Khop NAP[amount] %s | CK=%d credit=%d", oid, amt, _credit(o))
        return oid, fp

    return None, None


async def sepay_fetch(limit: int = 50) -> tuple[int, list[dict]]:
    global _sepay_auth_failed
    if not SEPAY_TOKEN or _sepay_auth_failed:
        return (401 if _sepay_auth_failed else 0), []
    headers = {"Authorization": f"Bearer {SEPAY_TOKEN}", "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                SEPAY_URL, headers=headers,
                params={"limit": limit}, timeout=HTTP_TIMEOUT,
            ) as r:
                body = await r.text()
                if r.status == 401:
                    _sepay_auth_failed = True
                    log.error("SePay 401 — doi SEPAY_TOKEN moi")
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
    ids = [_txn_id(t) for t in txns if _txn_id(t) > 0]
    return max(ids) if ids else 0


async def lock_old_txns() -> None:
    """
    FIX v4: CHI khoa GD da duoc xu ly (luu trong don paid).
    KHONG lay GD moi tu SePay ve khoa het — day la bug chinh
    gay mat tien khi bot restart trong luc co don cho.
    """
    cnt = 0
    for o in orders.values():
        fp = o.get("_txn_fp")
        if fp and o.get("paid") and fp not in processed_txns:
            processed_txns.add(fp)
            cnt += 1
    if cnt:
        _save_data()
    log.info("lock_old_txns: da sync %d GD tu don paid", cnt)


# ─────────────────────────────────────────────────────────────
# XAC NHAN THANH TOAN
# ─────────────────────────────────────────────────────────────

async def confirm_payment(oid: str, fp: str | None = None) -> None:
    o = orders.get(oid)
    if not o or o.get("paid"):
        return
    if fp and fp in processed_txns:
        return

    o["paid"]    = True
    o["paid_at"] = time.time()
    if fp:
        processed_txns.add(fp)
        o["_txn_fp"] = fp
    _save_data()

    uid    = int(o["user_id"])
    credit = _credit(o)
    bal    = add_balance(uid, credit)
    log.info("XAC NHAN %s | +%s VND | uid=%s | so_du=%s", oid, credit, uid, bal)

    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        em = discord.Embed(
            title="✅  Nạp tiền thành công!",
            description=(
                f"Đã cộng **`{credit:,}` VNĐ** vào ví.\n"
                f"Số dư hiện tại: **`{bal:,}` VNĐ**"
            ),
            color=C_GREEN,
        )
        em.add_field(name="📋 Mã đơn", value=f"`{oid}`", inline=True)
        em.add_field(name="⏰ Thời gian", value=_vn_now_str(), inline=True)
        em.set_footer(text="DucDuy Boutique · AimLock Pro Shop")
        await user.send(embed=em)
    except Exception as e:
        log.warning("Khong DM uid=%s: %s", uid, e)

    eph = o.get("ephemeral")
    if eph:
        try:
            wh = discord.Webhook.partial(int(eph["application_id"]), eph["token"], client=bot)
            ok = discord.Embed(
                title="✅  Nạp thành công!",
                description=(
                    f"Đã cộng `{credit:,}` VNĐ · Số dư `{bal:,}` VNĐ\n"
                    "📩 Chi tiết đã gửi vào DM."
                ),
                color=C_GREEN,
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
    pending = sum(1 for o in orders.values() if not o.get("paid") and not _order_expired(o))
    return web.json_response({
        "ok": True, "service": "ducduy-boutique-v4",
        "time_vn": _vn_now_str(), "pending_orders": pending,
    })


async def sepay_webhook(request: web.Request) -> web.Response:
    try:
        txn = _unwrap(await _parse_req(request))
        log.info("Webhook: amt=%s text=%s", _txn_amount(txn), _txn_text(txn)[:60])
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
    log.info("Webhook: %s/webhook (port %s)", PUBLIC_URL, WEBHOOK_PORT)


# ─────────────────────────────────────────────────────────────
# POLL SEPAY
# ─────────────────────────────────────────────────────────────

@tasks.loop(seconds=15)
async def poll_sepay() -> None:
    pending = [oid for oid, o in orders.items()
               if not o.get("paid") and not _order_expired(o)]
    if not pending or not SEPAY_TOKEN or _sepay_auth_failed:
        return
    st, txns = await sepay_fetch(50)
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
    """
    Giao dien chinh dung style Shop Clone.
    4 goi: 3 inline (hang 1) + 1 hang rieng (hang 2) giong anh mau.
    """
    pv   = PRODUCTS["aimlock_pro"]
    pkgs = pv["packages"]

    e = discord.Embed(
        title="🛒  AimLock Pro — Auto Buy",
        color=C_AIMLOCK,
    )

    # Dong dau: tieu de danh muc
    e.add_field(
        name="🔥  Danh mục đang bán",
        value="══════════════════════════════",
        inline=False,
    )

    # Hang 1: 3 goi dau (inline=True -> Discord xep 3 cot)
    for p in pkgs[:3]:
        e.add_field(
            name=f"🎯 {p['name']}",
            value=(
                f"💰 Giá: **{p['price']:,} VNĐ**\n"
                f"⏱️ Hạn: **{p['duration']}**"
            ),
            inline=True,
        )

    # Hang 2: goi thu 4 — inline=False -> xuong hang moi rieng
    e.add_field(
        name=f"🎯 {pkgs[3]['name']}",
        value=(
            f"💰 Giá: **{pkgs[3]['price']:,} VNĐ**\n"
            f"⏱️ Hạn: **{pkgs[3]['duration']}**"
        ),
        inline=False,
    )

    # Support
    e.add_field(
        name="📞  Support",
        value=(
            f"📩 Tạo Ticket: {SUPPORT_TEXT}\n"
            "👤 Hỗ trợ: Admin"
        ),
        inline=False,
    )

    e.add_field(
        name="\u200b",
        value="Vui lòng chọn danh mục bên dưới để tiếp tục",
        inline=False,
    )

    e.set_footer(text="💳 Thanh toán Tự Động · Nhanh Chóng · Uy Tín  |  DucDuy Boutique")

    if SHOP_THUMBNAIL:
        e.set_thumbnail(url=SHOP_THUMBNAIL)
    elif bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    if bot.user:
        e.set_author(name="DucDuy BTQ · AimLock Pro Shop", icon_url=bot.user.display_avatar.url)

    return e


def guide_embed() -> discord.Embed:
    e = discord.Embed(
        title="📖  Hướng dẫn sử dụng",
        description=(
            "**Bước 1 — Nạp tiền**\n"
            "Bấm `Nạp tiền` → nhập số VNĐ → quét QR → chuyển **đúng số CK** + mã `NAP...`\n\n"
            "**Bước 2 — Mua license**\n"
            "Menu `Chọn danh mục` → chọn gói AimLock Pro → điền số lượng → xác nhận\n\n"
            "**Bước 3 — Nhận key**\n"
            "Mở DM với bot — key gửi tự động trong vài giây\n\n"
            "══════════════════════\n"
            "⚠️ Chuyển **sau** khi tạo đơn nạp\n"
            "⚠️ Không làm tròn số tiền\n"
            "⚠️ Bật tin nhắn riêng từ thành viên server"
        ),
        color=C_PANEL,
    )
    e.set_footer(text="DUCDUY BOUTIQUE · Ho tro: " + SUPPORT_TEXT[:60])
    return e


def license_dm_embed(pkg: dict, keys: list[str]) -> discord.Embed:
    block = "\n".join(f"  {k}" for k in keys)
    e = discord.Embed(title="LICENSE AIMLOCK PRO", color=C_AIMLOCK)
    e.description = (
        "```fix\n"
        "--- LICENSE UNLOCKED ---\n"
        f"  {pkg['name']}\n"
        "------------------------\n"
        f"{block}\n"
        "```\n"
        f"Thoi han: **{pkg['duration']}**\n"
        "Khong chia se key · Mot thiet bi duy nhat"
    )
    e.set_footer(text="ducduy boutique · auto api")
    return e


# ── UI Components ──────────────────────────────────────────

class PackageSelect(discord.ui.Select):
    def __init__(self):
        pv = PRODUCTS["aimlock_pro"]
        super().__init__(
            placeholder="🎯  Chọn danh mục...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=f"{p['name']}  ·  {p['price']:,}d",
                    value=p["id"],
                    description=f"Thoi han {p['duration']}",
                    emoji="🎯",
                )
                for p in pv["packages"]
            ],
            custom_id="boutique_pkg_select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BuyModal(self.values[0]))


class ShopPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PackageSelect())

    @discord.ui.button(
        label="Nap tien", emoji="💳",
        style=discord.ButtonStyle.success,
        custom_id="boutique_deposit", row=1,
    )
    async def deposit(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(DepositModal())

    @discord.ui.button(
        label="So du", emoji="💰",
        style=discord.ButtonStyle.primary,
        custom_id="boutique_balance", row=1,
    )
    async def balance(self, interaction: discord.Interaction, _):
        bal = get_balance(interaction.user.id)
        em  = discord.Embed(
            title="So du vi Boutique",
            description=f"```fix\n{bal:,} VND\n```",
            color=C_SHOP,
        )
        em.set_footer(text="Nap them tai nut Nap tien")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @discord.ui.button(
        label="Huong Dan", emoji="📖",
        style=discord.ButtonStyle.secondary,
        custom_id="boutique_guide", row=1,
    )
    async def guide_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_message(embed=guide_embed(), ephemeral=True)


class DepositModal(discord.ui.Modal, title="Nap tien"):
    amount = discord.ui.TextInput(
        label="So tien (VND)",
        placeholder="Vi du: 50000",
        min_length=4, max_length=9,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.replace(",", "").replace(".", "").strip()
        try:
            base = int(raw)
        except ValueError:
            return await interaction.response.send_message(
                "So tien khong hop le.", ephemeral=True
            )
        if base < MIN_DEPOSIT:
            return await interaction.response.send_message(
                f"Toi thieu {MIN_DEPOSIT:,} VND.", ephemeral=True
            )
        if not BANK_NUMBER:
            return await interaction.response.send_message(
                "Chua cau hinh BANK_NUMBER.", ephemeral=True
            )

        since           = await sepay_latest_id() if SEPAY_TOKEN else 0
        oid, base, transfer = create_order(interaction.user.id, base, since)

        await interaction.response.send_message(
            embed=deposit_embed(base, transfer, oid), ephemeral=True
        )
        try:
            msg = await interaction.original_response()
            orders[oid]["ephemeral"] = {
                "application_id": interaction.application_id,
                "token":          interaction.token,
                "message_id":     msg.id,
            }
            _save_data()
        except Exception:
            pass


class BuyModal(discord.ui.Modal):
    qty = discord.ui.TextInput(label="So luong key", default="1", max_length=2)

    def __init__(self, pkg_id: str):
        self.pkg_id = pkg_id
        p = PKG[pkg_id]
        super().__init__(title=f"Mua {p['name']}")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            q = max(1, int(self.qty.value.strip()))
        except ValueError:
            return await interaction.response.send_message(
                "So luong khong hop le.", ephemeral=True
            )

        p     = PKG[self.pkg_id]
        total = p["price"] * q
        uid   = interaction.user.id
        bal   = get_balance(uid)

        if bal < total:
            return await interaction.response.send_message(
                f"Khong du tien.\n"
                f"So du: {bal:,} · Can: {total:,} · Thieu: {total - bal:,} VND",
                ephemeral=True,
            )
        if not API_ADMIN_USER or not API_ADMIN_PASS:
            return await interaction.response.send_message(
                "Bot chua cau hinh API admin.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="Dang tao license...",
                description=(
                    f"AimLock Pro · Goi {p['duration']} x{q}\n"
                    "Vui long doi 10-30 giay (Render can wake up)."
                ),
                color=C_AIMLOCK,
            )
        )

        deduct_balance(uid, total)
        keys: list[str] = []
        for _ in range(q):
            k = await api_create_key(p["product_key"], p, uid)
            if k:
                keys.append(k)
            else:
                add_balance(uid, p["price"] * (q - len(keys)))
                break

        new_bal = get_balance(uid)
        paid    = p["price"] * len(keys)

        if keys:
            em = discord.Embed(
                title="GIAO DICH HOAN TAT",
                description=(
                    f"{p['name']}\n"
                    f"Thoi han: {p['duration']} · So key: {len(keys)}\n"
                    f"Tru: {paid:,} VND · Vi con: {new_bal:,} VND\n\n"
                    "Key da gui vao DM!"
                ),
                color=C_AIMLOCK,
            )
        else:
            em = discord.Embed(
                title="Khong tao duoc key",
                description=(
                    "API server dang ngu hoac loi.\n"
                    "Da hoan tien vao vi. Thu lai sau 1 phut."
                ),
                color=0xE74C3C,
            )

        await interaction.edit_original_response(embed=em)

        if keys:
            try:
                await interaction.user.send(embed=license_dm_embed(p, keys))
            except discord.Forbidden:
                await interaction.followup.send(
                    "Bat DM de nhan key!", ephemeral=True
                )


# ─────────────────────────────────────────────────────────────
# LENH ADMIN
# ─────────────────────────────────────────────────────────────

@bot.command(name="shop", aliases=["menu", "s"])
async def cmd_shop(ctx: commands.Context):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(embed=hub_embed(), view=ShopPanelView())


@bot.command()
@commands.has_permissions(administrator=True)
async def xacnhan(ctx: commands.Context, oid: str):
    oid = oid.upper()
    if oid not in orders:
        return await ctx.send(f"Khong co don `{oid}`.")
    if orders[oid].get("paid"):
        return await ctx.send(f"Don `{oid}` da xac nhan roi.")
    await confirm_payment(oid)
    await ctx.send(f"Da xac nhan `{oid}`.")


@bot.command()
@commands.has_permissions(administrator=True)
async def congcoin(ctx: commands.Context, member: discord.Member, amount: int):
    bal = add_balance(member.id, amount)
    await ctx.send(f"Cong {amount:,} VND cho {member.mention} · So du: {bal:,}")


@bot.command()
@commands.has_permissions(administrator=True)
async def trucoin(ctx: commands.Context, member: discord.Member, amount: int):
    if not deduct_balance(member.id, amount):
        return await ctx.send("So du khong du.")
    await ctx.send(f"Tru {amount:,} VND · So du: {get_balance(member.id):,}")


@bot.command()
@commands.has_permissions(administrator=True)
async def doncho(ctx: commands.Context):
    pending = [(oid, o) for oid, o in orders.items()
               if not o.get("paid") and not _order_expired(o)]
    if not pending:
        return await ctx.send("Khong co don cho.")
    lines = [
        f"`{oid}` CK `{_transfer(o):,}` +`{_credit(o):,}` <@{o['user_id']}>"
        for oid, o in pending[:15]
    ]
    await ctx.send(embed=discord.Embed(
        title=f"Don cho ({len(pending)})",
        description="\n".join(lines),
        color=0xFFAA00,
    ))


@bot.command()
@commands.has_permissions(administrator=True)
async def info(ctx: commands.Context):
    sepay   = "OK" if SEPAY_TOKEN and not _sepay_auth_failed else ("401" if _sepay_auth_failed else "chua cau hinh")
    pending = sum(1 for o in orders.values() if not o.get("paid"))
    api_ok  = "OK" if API_ADMIN_USER and API_ADMIN_PASS else "THIEU"
    await ctx.send(
        f"Bot: {bot.user}\n"
        f"Webhook: {PUBLIC_URL}/webhook\n"
        f"SePay: {sepay}\n"
        f"Don cho: {pending}\n"
        f"Gio VN: {_vn_now_str()}\n"
        f"API [{api_ok}]: {API_AIMLOCK_BASE}"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def sepaycheck(ctx: commands.Context):
    if not SEPAY_TOKEN:
        return await ctx.send("Chua co SEPAY_TOKEN.")
    st, txns = await sepay_fetch(8)
    if st == 401:
        return await ctx.send("SePay 401 — doi token moi tren Render.")
    if st != 200:
        return await ctx.send(f"SePay HTTP {st}")
    lines = ["GD gan nhat (limit 8):"]
    for t in txns[:5]:
        lines.append(f"`{_txn_date(t)}` +{_txn_amount(t):,} `{_txn_text(t)[:40]}`")
    if not txns:
        lines.append("Khong co GD nao")
    await ctx.send("\n".join(lines))


@bot.command()
@commands.has_permissions(administrator=True)
async def sepayreset(ctx: commands.Context):
    global _sepay_auth_failed
    _sepay_auth_failed = False
    await ctx.send("Da reset SePay. Thu !sepaycheck.")


@bot.command()
@commands.has_permissions(administrator=True)
async def testkey(ctx: commands.Context, pkg_id: str = "ap_1d"):
    if pkg_id not in PKG:
        return await ctx.send("Goi khong ton tai. Dung: ap_1d ap_7d ap_1m ap_ob")
    await ctx.send(f"Dang tao key {pkg_id}...", delete_after=5)
    k = await api_create_key(PKG[pkg_id]["product_key"], PKG[pkg_id], ctx.author.id)
    if k:
        await ctx.send(f"Key: {k}", delete_after=30)
    else:
        await ctx.send("Loi API — xem log Render", delete_after=15)


# ─────────────────────────────────────────────────────────────
# KHOI DONG
# ─────────────────────────────────────────────────────────────

_http_started = False


@bot.event
async def on_ready():
    global _http_started
    log.info("=== ONLINE: %s | VN %s ===", bot.user, _vn_now_str())
    bot.add_view(ShopPanelView())
    if not _http_started:
        await start_http()
        _http_started = True
    if not poll_sepay.is_running():
        poll_sepay.start()
    await lock_old_txns()
    if not SEPAY_TOKEN:
        log.warning("THIEU SEPAY_TOKEN — khong tu dong cong tien!")
    else:
        log.info("SePay OK — poll 15s + webhook %s/webhook", PUBLIC_URL)
    if not API_ADMIN_USER or not API_ADMIN_PASS:
        log.warning("THIEU API_ADMIN_USER/PASS — khong tao duoc key!")
    else:
        log.info("AimLock API: %s", API_AIMLOCK_BASE)


if not TOKEN:
    raise SystemExit("Thieu DISCORD_TOKEN trong file .env")

bot.run(TOKEN)
