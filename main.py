import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web
import aiohttp
import asyncio
import os
import re
import random
import json
import logging
import time
import datetime
from pathlib import Path

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

# ══════════════════════════════════════════
# LOAD ENV
# ══════════════════════════════════════════

def _clean_env(value: str | None) -> str:
    if not value:
        return ""
    v = value.strip().strip('"').strip("'")
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v

for _name in (".env", "env"):
    _p = Path(__file__).resolve().parent / _name
    if _p.exists():
        load_dotenv(_p)
        break
else:
    load_dotenv()

TOKEN          = _clean_env(os.getenv("DISCORD_TOKEN"))
BANK_NUMBER    = _clean_env(os.getenv("BANK_NUMBER"))
BANK_NAME      = _clean_env(os.getenv("BANK_NAME", "msb")) or "msb"
ACCOUNT_NAME   = _clean_env(os.getenv("ACCOUNT_NAME", "DUCDUY BOUTIQUE"))
BANK_DISPLAY   = _clean_env(os.getenv("BANK_DISPLAY", "MSB Bank"))
SEPAY_TOKEN    = _clean_env(os.getenv("SEPAY_TOKEN") or os.getenv("SEPAY_API_KEY"))
ORDER_EXPIRE   = int(os.getenv("ORDER_EXPIRE_MINUTES", "15")) * 60
PUBLIC_URL     = _clean_env(
    os.getenv("PUBLIC_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://shopboutique.onrender.com"
)
WEBHOOK_PORT   = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT") or "8080")
SHOP_THUMBNAIL = _clean_env(os.getenv("SHOP_THUMBNAIL", ""))
SUPPORT_TEXT   = _clean_env(os.getenv("SUPPORT_TEXT", "Ticket server · DM admin"))
DEPOSIT_MSG_TTL = int(os.getenv("DEPOSIT_MSG_TTL", "120"))

# Theme — Boutique Nexus (không dùng layout shop clone)
C_NEXUS   = 0xF5C451
C_PANEL   = 0x12151C
C_OK      = 0x3DFFA8
C_LEGIT   = 0x3DFFA8
C_AIMBOT  = 0xFF4FD8
C_MUTED   = 0x7A8499

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shop")

_sepay_auth_failed = False

# ══════════════════════════════════════════
# BOT SETUP
# ══════════════════════════════════════════

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ══════════════════════════════════════════
# PERSISTENT STORAGE
# ══════════════════════════════════════════

DATA_FILE = "data.json"

def _load_data():
    global balances, orders, processed_txns
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d             = json.load(f)
            balances      = {int(k): v for k, v in d.get("balances", {}).items()}
            orders        = d.get("orders", {})
            processed_txns = set(str(x) for x in d.get("processed_txns", []))
            pending       = len([o for o in orders.values() if not o.get("paid")])
            log.info("Loaded %d don (%d cho), %d user", len(orders), pending, len(balances))
    except FileNotFoundError:
        log.info("Chua co data.json, bat dau moi")
    except Exception as e:
        log.error("Load data loi: %s", e)

def _save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "balances": balances,
                    "orders": orders,
                    "processed_txns": sorted(processed_txns)[-5000:],
                },
                f,
                ensure_ascii=False,
            )
    except Exception as e:
        log.error("Save data loi: %s", e)

balances: dict[int, int] = {}
orders: dict[str, dict] = {}
processed_txns: set[str] = set()
_load_data()

# ══════════════════════════════════════════
# DANH MUC SAN PHAM
# ══════════════════════════════════════════

PRODUCTS = {
    "legit_drag": {
        "label": "Legit Drag",
        "emoji": "🎯",
        "tagline": "Ghim Ngực - Kéo Tâm Dễ Dàng - Phù Hợp Chơi Chay",
        "accent": C_LEGIT,
        "packages": [
            {"id": "ld_3h",  "name": "Legit Drag 3 Gio",   "price":   3_000, "duration": "3 gio",   "days": 1},
            {"id": "ld_1d",  "name": "Legit Drag 1 Ngay",  "price":  10_000, "duration": "1 ngay",  "days": 1},
            {"id": "ld_7d",  "name": "Legit Drag 7 Ngay",  "price":  50_000, "duration": "7 ngay",  "days": 7},
            {"id": "ld_1m",  "name": "Legit Drag 1 Thang", "price": 120_000, "duration": "1 thang", "days": 30},
            {"id": "ld_1ob", "name": "Legit Drag 1 OB",    "price": 240_000, "duration": "1 OB",    "days": 90},
        ],
    },
    "aimbot_head": {
        "label": "Aimbot Head",
        "emoji": "🔫",
        "tagline": "Ghim Đầu Chặt - Không Lỗi Dame - Dễ Sử Dụng",
        "accent": C_AIMBOT,
        "packages": [
            {"id": "ah_3h",  "name": "Aimbot Head 3 Gio",   "price":   5_000, "duration": "3 gio",   "days": 1},
            {"id": "ah_1d",  "name": "Aimbot Head 1 Ngay",  "price":  15_000, "duration": "1 ngay",  "days": 1},
            {"id": "ah_7d",  "name": "Aimbot Head 7 Ngay",  "price":  60_000, "duration": "7 ngay",  "days": 7},
            {"id": "ah_1m",  "name": "Aimbot Head 1 Thang", "price": 240_000, "duration": "1 thang", "days": 30},
            {"id": "ah_1ob", "name": "Aimbot Head 1 OB",    "price": 450_000, "duration": "1 OB",    "days": 90},
        ],
    },
}

PKG: dict[str, dict] = {}
for _pk, _pv in PRODUCTS.items():
    for _pkg in _pv["packages"]:
        PKG[_pkg["id"]] = {**_pkg, "product_key": _pk, "product_label": _pv["label"]}

def _min_price(product_key: str) -> int:
    return min(p["price"] for p in PRODUCTS[product_key]["packages"])

# ══════════════════════════════════════════
# KHO KEY (FILE) — keys/<ma_goi>.txt, moi dong 1 key
# ══════════════════════════════════════════

KEYS_DIR = Path(__file__).resolve().parent / "keys"
_key_file_lock = asyncio.Lock()

def _init_keys_dir():
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    for pkg_id in PKG:
        p = KEYS_DIR / (pkg_id + ".txt")
        if not p.exists():
            p.write_text(
                "# File key cho goi: " + pkg_id + "\n"
                "# Moi dong 1 key. Dong # la ghi chu.\n",
                encoding="utf-8",
            )

def _key_file_path(pkg_id: str) -> Path:
    return KEYS_DIR / (pkg_id + ".txt")

def _read_key_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]

def count_keys(pkg_id: str) -> int:
    return len(_read_key_lines(_key_file_path(pkg_id)))

def count_keys_product(product_key: str) -> int:
    return sum(count_keys(p["id"]) for p in PRODUCTS[product_key]["packages"])

def count_keys_total() -> int:
    return sum(count_keys(pid) for pid in PKG)

def _pop_key_sync(pkg_id: str) -> str | None:
    path = _key_file_path(pkg_id)
    keys = _read_key_lines(path)
    if not keys:
        return None
    key = keys[0]
    rest = keys[1:]
    path.write_text(
        ("\n".join(rest) + "\n") if rest else "# Het key — them key moi vao file nay\n",
        encoding="utf-8",
    )
    log.info("Lay key %s | con lai %d", pkg_id, len(rest))
    return key

async def take_key(pkg_id: str) -> str | None:
    async with _key_file_lock:
        return await asyncio.to_thread(_pop_key_sync, pkg_id)

def _restore_keys_sync(pkg_id: str, keys: list[str]) -> None:
    if not keys:
        return
    path = _key_file_path(pkg_id)
    existing = _read_key_lines(path)
    path.write_text("\n".join(keys + existing) + "\n", encoding="utf-8")
    log.info("Hoan %d key vao %s", len(keys), pkg_id)

async def restore_keys(pkg_id: str, keys: list[str]) -> None:
    async with _key_file_lock:
        await asyncio.to_thread(_restore_keys_sync, pkg_id, keys)

def stock_summary_text() -> str:
    lines = []
    for pk, pv in PRODUCTS.items():
        parts = []
        for p in pv["packages"]:
            c = count_keys(p["id"])
            icon = "🟢" if c > 5 else ("🟡" if c else "🔴")
            parts.append(icon + " `" + p["duration"] + "`: **" + str(c) + "**")
        total = count_keys_product(pk)
        lines.append(pv["emoji"] + " **" + pv["label"] + "** — `" + str(total) + "` key\n" + " · ".join(parts))
    lines.append("\n**Tổng kho:** `" + str(count_keys_total()) + "` key")
    return "\n".join(lines)

_init_keys_dir()

def _fmt_vnd(n: int) -> str:
    return "{:,}".format(n) + "₫"

HTTP_HEADERS = {
    "User-Agent": "DucDuyBoutique-ShopBot/2.0",
    "Accept": "application/json",
}
API_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=20)

def _short_body(body: str, limit: int = 220) -> str:
    t = re.sub(r"\s+", " ", (body or "")).strip()
    if len(t) > limit:
        return t[:limit] + "..."
    return t

def _is_cloudflare_html(body: str) -> bool:
    b = (body or "").lower()
    return "cloudflare" in b and ("cf-ray" in b or "error code" in b or "<!doctype html" in b)

def _cf_ray_from_body(body: str) -> str:
    m = re.search(r"Ray ID[:\s]*<?[^>]*>?([a-f0-9]{16})", body or "", re.I)
    return m.group(1) if m else ""

def _log_http_fail(label: str, url: str, status: int, body: str):
    if _is_cloudflare_html(body):
        ray = _cf_ray_from_body(body)
        log.error(
            "%s | %s | HTTP %s | CLOUDFLARE (Ray %s) — API dang ngu/qua tai. "
            "Doi 30-60s hoac mo URL tren trinh duyet de danh thuc Render.",
            label, url, status, ray or "?",
        )
    elif status in (502, 503, 520, 521, 522, 524):
        log.error("%s | %s | HTTP %s | Server tam thoi loi: %s", label, url, status, _short_body(body))
    else:
        log.error("%s | %s | HTTP %s | %s", label, url, status, _short_body(body))

async def _read_http(resp: aiohttp.ClientResponse) -> tuple[str, dict]:
    body = await resp.text()
    data: dict = {}
    if body.strip().startswith("{"):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            pass
    return body, data

# ══════════════════════════════════════════
# HAM TIEN ICH
# ══════════════════════════════════════════

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

def make_order_id() -> str:
    oid = "NAP" + str(int(time.time()))
    n = 0
    while oid in orders:
        n += 1
        oid = "NAP" + str(int(time.time())) + str(n)
    return oid

def _order_transfer_amount(order: dict) -> int:
    """Số tiền bắt buộc phải chuyển (unique)."""
    return int(order.get("transfer_amount") or order.get("amount") or 0)

def _order_credit_amount(order: dict) -> int:
    """Số tiền thực cộng vào ví."""
    return int(order.get("base_amount") or order.get("amount") or 0)

def _pending_transfer_amounts() -> set[int]:
    s = set()
    for o in orders.values():
        if not o.get("paid") and not _order_expired(o):
            s.add(_order_transfer_amount(o))
    return s

def _alloc_unique_transfer_amount(base_amount: int) -> int:
    """Mỗi đơn +1..999đ để nhiều người nạp cùng mức vẫn khớp chính xác."""
    used = _pending_transfer_amounts()
    for offset in range(1, 1000):
        t = base_amount + offset
        if t not in used:
            return t
    return base_amount + random.randint(1000, 9999)

def create_deposit_order(user_id: int, base_amount: int) -> tuple[str, int, int]:
    order_id = make_order_id()
    transfer_amount = _alloc_unique_transfer_amount(base_amount)
    orders[order_id] = {
        "user_id":         user_id,
        "base_amount":     base_amount,
        "transfer_amount": transfer_amount,
        "amount":          transfer_amount,
        "paid":            False,
        "created_at":      time.time(),
    }
    return order_id, base_amount, transfer_amount

def _order_expired(order: dict) -> bool:
    created = order.get("created_at", 0)
    return (time.time() - created) > ORDER_EXPIRE

def build_qr_url(amount: int, order_id: str) -> str:
    bank = BANK_NAME.lower().strip()
    if bank == "msbbank":
        bank = "msb"
    name = ACCOUNT_NAME.replace(" ", "%20")
    return (
        "https://img.vietqr.io/image/" + bank + "-" + str(BANK_NUMBER) + "-compact2.png"
        + "?amount=" + str(amount)
        + "&addInfo=" + order_id
        + "&accountName=" + name
    )

def build_deposit_embed(base_amount: int, transfer_amount: int, order_id: str) -> discord.Embed:
    e = discord.Embed(
        title="💳  Thông tin nạp tiền",
        description=(
            "⚠️ Phải chuyển **đúng số tiền bên dưới** (có thêm vài đồng để hệ thống nhận diện đơn của bạn).\n"
            "Sau khi nhận tiền, bot cộng **`{:,}` VNĐ** vào ví.".format(base_amount)
        ),
        color=C_NEXUS,
    )
    e.add_field(
        name="💵  Thông tin nạp",
        value=(
            "**Số tiền PHẢI chuyển:** `" + "{:,}".format(transfer_amount) + " VNĐ`\n"
            + "**Sẽ cộng vào ví:** `" + "{:,}".format(base_amount) + " VNĐ`\n"
            + "**Mã đơn:** `" + order_id + "`\n"
            + "**Nội dung CK:** `" + order_id + "`"
        ),
        inline=False,
    )
    e.add_field(
        name="🏛️  Thông tin tài khoản",
        value=(
            "**Chủ tài khoản:** " + ACCOUNT_NAME + "\n"
            + "**Ngân hàng:** " + BANK_DISPLAY + "\n"
            + "**Số tài khoản:** `" + str(BANK_NUMBER) + "`"
        ),
        inline=False,
    )
    e.add_field(name="📌  Trạng thái", value="⏳  **Chờ thanh toán**", inline=False)
    e.add_field(
        name="💡  Hướng dẫn",
        value=(
            "1️⃣  Quét **mã QR** bên dưới *(khuyến nghị)* hoặc chuyển thủ công\n"
            + "2️⃣  Nhập **đúng số tiền:** `" + "{:,}".format(transfer_amount) + " VNĐ` *(không làm tròn)*\n"
            + "3️⃣  Nhập **nội dung:** `" + order_id + "`\n"
            + "4️⃣  Bot tự cộng **`{:,}` VNĐ** vào ví trong ~1–2 phút".format(base_amount)
        ),
        inline=False,
    )
    e.set_image(url=build_qr_url(transfer_amount, order_id))
    e.set_footer(
        text="ducduy boutique  •  Hết hạn sau "
        + str(ORDER_EXPIRE // 60)
        + " phút  •  "
        + order_id
    )
    return e

def _parse_amount(val) -> int:
    try:
        return int(float(val or 0))
    except (ValueError, TypeError):
        return 0

def _get_txn_amount(txn: dict) -> int:
    val = txn.get("transferAmount") or txn.get("amount_in") or txn.get("amount") or 0
    return _parse_amount(val)

def _unwrap_txn(body) -> dict:
    if not isinstance(body, dict):
        return {}
    for key in ("transaction", "data", "payload", "body"):
        inner = body.get(key)
        if isinstance(inner, dict) and (
            inner.get("transferAmount") is not None
            or inner.get("amount_in") is not None
            or inner.get("content")
        ):
            return inner
    return body

def _get_txn_text(txn: dict) -> str:
    parts = [
        str(txn.get("transaction_content") or ""),
        str(txn.get("content") or ""),
        str(txn.get("description") or ""),
        str(txn.get("code") or ""),
        str(txn.get("reference_number") or ""),
        str(txn.get("referenceCode") or ""),
        str(txn.get("sub_account") or ""),
        str(txn.get("subAccount") or ""),
    ]
    return " ".join(parts).upper()

def _order_id_in_text(oid: str, text: str) -> bool:
    if not text:
        return False
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    oid_up = oid.upper()
    if oid_up in text.upper() or oid_up in compact:
        return True
    digits = oid_up.replace("NAP", "")
    if len(digits) >= 8 and digits in compact:
        return True
    for m in re.findall(r"NAP\d{8,}", compact):
        if m == oid_up:
            return True
    return False

def _get_txn_date(txn: dict) -> str:
    return str(txn.get("transactionDate") or txn.get("transaction_date") or "")

def _txn_timestamp(txn: dict, order_created: float = 0) -> float:
    """Parse thoi gian giao dich — SePay co the gui gio VN hoac UTC."""
    s = _get_txn_date(txn)
    if not s:
        return time.time()
    candidates: list[float] = []
    for tz in (VN_TZ, datetime.timezone.utc):
        try:
            dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            candidates.append(dt.timestamp())
        except Exception:
            pass
    if not candidates:
        return time.time()
    if order_created > 0:
        return min(candidates, key=lambda t: abs(t - order_created))
    return candidates[0]

def _txn_fingerprint(txn: dict) -> str:
    tid = str(txn.get("id") or "").strip()
    if tid and tid not in ("0", "None"):
        return "id:" + tid
    ref = str(txn.get("referenceCode") or txn.get("reference_number") or "").strip()
    if ref:
        return "ref:" + ref
    return "fp:" + _get_txn_date(txn) + "|" + str(_get_txn_amount(txn)) + "|" + _get_txn_text(txn)[:80]

def _is_incoming(txn: dict) -> bool:
    """SePay list API thường không có transferType — dùng amount_in."""
    t = txn.get("transferType")
    if t is not None and str(t).lower() == "out":
        return False
    if t is not None and str(t).lower() == "in":
        return True
    try:
        if float(txn.get("amount_in") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return _get_txn_amount(txn) > 0

def _find_order_for_txn(txn: dict) -> tuple[str | None, str | None]:
    """Khớp theo số tiền CK unique (mỗi đơn một số khác nhau)."""
    fp = _txn_fingerprint(txn)
    if fp in processed_txns:
        return None, None

    if not _is_incoming(txn):
        return None, None

    amount = _get_txn_amount(txn)
    if amount <= 0:
        return None, None

    text = _get_txn_text(txn)

    for oid, order in sorted(
        orders.items(),
        key=lambda x: x[1].get("created_at", 0),
        reverse=True,
    ):
        if order.get("paid") or _order_expired(order):
            continue
        need = _order_transfer_amount(order)
        if amount != need:
            continue
        credit = _order_credit_amount(order)
        if _order_id_in_text(oid, text):
            log.info("Khop UNIQUE+MA %s | CK %d | +%d | %.40s", oid, need, credit, text)
        else:
            log.info("Khop UNIQUE %s | CK %d | +%d | %.40s", oid, need, credit, text)
        return oid, fp

    return None, None

async def _sepay_get(params: dict | None = None) -> tuple[int, dict]:
    """Goi SePay API; tra (status, json)."""
    global _sepay_auth_failed
    if not SEPAY_TOKEN:
        return 0, {}
    if _sepay_auth_failed:
        return 401, {}

    headers = {**HTTP_HEADERS, "Authorization": "Bearer " + SEPAY_TOKEN}
    url = "https://my.sepay.vn/userapi/transactions/list"
    try:
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as s:
            async with s.get(
                url,
                headers=headers,
                params=params or {"limit": 20},
                timeout=API_TIMEOUT,
            ) as r:
                body, data = await _read_http(r)
                if r.status == 401:
                    _sepay_auth_failed = True
                    log.error("SePay 401 — kiem tra SEPAY_TOKEN tren Render. %s", _short_body(body))
                    return 401, {}
                if r.status != 200 or _is_cloudflare_html(body):
                    if _is_cloudflare_html(body):
                        _log_http_fail("SePay", url, r.status, body)
                    else:
                        log.warning("SePay HTTP %s: %s", r.status, _short_body(body))
                    return r.status, {}
                _sepay_auth_failed = False
                if not data and body.strip():
                    log.warning("SePay tra khong phai JSON: %s", _short_body(body))
                return r.status, data
    except asyncio.TimeoutError:
        log.error("SePay timeout — thu lai sau hoac kiem tra mang")
        return 0, {}
    except Exception as e:
        log.error("SePay request loi: %s", e)
        return 0, {}

async def confirm_payment(order_id: str, txn_fp: str | None = None):
    order = orders.get(order_id)
    if not order or order.get("paid"):
        return
    if txn_fp and txn_fp in processed_txns:
        log.info("Bo qua txn %s — da xu ly", txn_fp)
        return

    uid = order.get("user_id")
    if not uid:
        log.error("Don %s khong co user_id", order_id)
        return

    order["paid"] = True
    order["paid_at"] = time.time()
    if txn_fp:
        processed_txns.add(str(txn_fp))
    _save_data()

    credit = _order_credit_amount(order)
    bal    = add_balance(uid, credit)
    log.info(
        "XAC NHAN %s | CK %d | +%d | user %s | du %s",
        order_id, _order_transfer_amount(order), credit, uid, bal,
    )

    try:
        user = await bot.fetch_user(uid)
        embed = discord.Embed(
            title="✅  Nạp tiền thành công!",
            description="Giao dịch đã được xác nhận tự động.",
            color=0x2ECC71,
        )
        embed.add_field(name="💵  Đã nạp", value="`" + "{:,}".format(credit) + " VNĐ`", inline=True)
        embed.add_field(name="💰  Số dư", value="`" + "{:,}".format(bal) + " VNĐ`", inline=True)
        embed.add_field(name="🧾  Mã đơn", value="`" + order_id + "`", inline=False)
        embed.add_field(
            name="👉  Tiếp theo",
            value="Quay lại shop → chọn **lane** → mua key qua DM.",
            inline=False,
        )
        embed.set_footer(text="ducduy boutique")
        await user.send(embed=embed)
    except Exception as e:
        log.warning("Khong DM duoc user %s: %s", uid, e)

    await _replace_deposit_message(order_id, credit, bal)

def _deposit_success_embed(amount: int, balance: int) -> discord.Embed:
    e = discord.Embed(
        title="✅  Nạp tiền thành công!",
        description=(
            "Hệ thống đã cộng tiền vào ví của bạn.\n\n"
            "📩 **Vui lòng check DM** để xem chi tiết giao dịch.\n"
            "🛒 Quay lại shop → chọn danh mục → mua key."
        ),
        color=0x2ECC71,
    )
    e.add_field(name="💵  Đã nạp", value="`{:,}` VNĐ".format(amount), inline=True)
    e.add_field(name="💰  Số dư", value="`{:,}` VNĐ".format(balance), inline=True)
    e.set_footer(text="Tin nhắn này sẽ tự xóa sau ~2 phút")
    return e

async def _replace_deposit_message(order_id: str, amount: int, balance: int):
    order = orders.get(order_id)
    if not order:
        return
    eph = order.get("ephemeral")
    if not eph:
        return
    try:
        webhook = discord.Webhook.partial(
            int(eph["application_id"]),
            eph["token"],
            client=bot,
        )
        await webhook.edit_message(
            int(eph["message_id"]),
            embed=_deposit_success_embed(amount, balance),
            attachments=[],
        )
        asyncio.create_task(
            _delete_ephemeral_later(
                eph["application_id"],
                eph["token"],
                eph["message_id"],
                DEPOSIT_MSG_TTL,
            )
        )
        log.info("Da thay tin nap thanh cong cho don %s", order_id)
    except discord.NotFound:
        log.debug("Tin nap ephemeral da mat — don %s", order_id)
    except Exception as e:
        log.warning("Khong sua duoc tin nap ephemeral %s: %s", order_id, e)

async def _delete_ephemeral_later(app_id, token: str, message_id, delay: int):
    await asyncio.sleep(delay)
    try:
        webhook = discord.Webhook.partial(int(app_id), token, client=bot)
        await webhook.delete_message(int(message_id))
    except Exception:
        pass

@tasks.loop(seconds=10)
async def poll_sepay():
    pending = [
        oid for oid, o in orders.items()
        if not o.get("paid") and not _order_expired(o)
    ]
    if not pending:
        return
    if not SEPAY_TOKEN:
        return
    if _sepay_auth_failed:
        log.warning("Poll dung — SEPAY 401, can cap nhat SEPAY_TOKEN tren Render")
        return

    params = {"limit": 80}
    if BANK_NUMBER:
        params["account_number"] = BANK_NUMBER
    status, data = await _sepay_get(params)
    if status != 200:
        return

    txns = data.get("transactions", [])
    matched_any = False
    for txn in txns:
        oid, fp = _find_order_for_txn(txn)
        if oid:
            matched_any = True
            await confirm_payment(oid, fp)

    if pending and not matched_any and txns:
        t0 = txns[0]
        need_map = {
            o: _order_transfer_amount(orders[o])
            for o in pending
        }
        log.info(
            "Poll chua khop | pending=%s | txn=%s | can=%s | text=%.50s",
            pending,
            _get_txn_amount(t0),
            need_map,
            _get_txn_text(t0),
        )

async def _parse_webhook_request(request: web.Request) -> dict:
    ctype = (request.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        raw = await request.json()
        return _unwrap_txn(raw) if isinstance(raw, dict) else {}
    if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        post = await request.post()
        flat = {k: (v[0] if isinstance(v, (list, tuple)) else v) for k, v in post.items()}
        return _unwrap_txn(flat)
    text = await request.text()
    if not text:
        return {}
    try:
        raw = json.loads(text)
        return _unwrap_txn(raw) if isinstance(raw, dict) else {}
    except json.JSONDecodeError:
        log.warning("Webhook body khong parse duoc: %s", text[:300])
        return {}

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "ducduy-boutique"})

async def handle_webhook(request: web.Request) -> web.Response:
    try:
        body = _unwrap_txn(await _parse_webhook_request(request))
        amt = _get_txn_amount(body)
        text = _get_txn_text(body)
        log.info(
            "Webhook: id=%s amount=%s type=%s | content=%.80s",
            body.get("id"), amt, body.get("transferType"), text,
        )

        oid, fp = _find_order_for_txn(body)
        if oid:
            log.info("Webhook khop don %s", oid)
            await confirm_payment(oid, fp)
            return web.json_response({"success": True})

        pending = [
            o for o, ord in orders.items()
            if not ord.get("paid") and not _order_expired(ord)
        ]
        if pending:
            log.warning(
                "Webhook KHONG KHOP | amount=%s | pending=%s | text=%.100s",
                amt, pending, text,
            )

        return web.json_response({"success": True})
    except json.JSONDecodeError:
        return web.json_response({"success": False}, status=400)
    except Exception as e:
        log.error("Webhook loi: %s", e, exc_info=True)
        return web.json_response({"success": False}, status=500)

async def start_webhook_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_post("/webhook", handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT).start()
    log.info("Webhook %s/webhook (port %d)", PUBLIC_URL.rstrip("/"), WEBHOOK_PORT)

# ══════════════════════════════════════════
# MODAL NAP TIEN
# ══════════════════════════════════════════

class DepositModal(discord.ui.Modal, title="💳  Nạp tiền"):
    amount = discord.ui.TextInput(
        label="Số tiền muốn nạp (VNĐ)",
        placeholder="Ví dụ: 50000",
        min_length=4,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount.value.replace(",", "").replace(".", "").strip()
        try:
            amount = int(raw)
        except ValueError:
            return await interaction.response.send_message("❌ Số tiền không hợp lệ.", ephemeral=True)
        if amount < 1_000:
            return await interaction.response.send_message(
                "❌ Số tiền tối thiểu là **1.000 VNĐ**.", ephemeral=True
            )

        order_id, base_amount, transfer_amount = create_deposit_order(
            interaction.user.id, amount
        )
        _save_data()
        log.info(
            "Tao don: %s | CK %d | +%d | user %s",
            order_id, transfer_amount, base_amount, interaction.user.id,
        )

        if not BANK_NUMBER:
            return await interaction.response.send_message(
                "❌ Bot chưa cấu hình `BANK_NUMBER` trên server. Liên hệ admin.",
                ephemeral=True,
            )

        embed = build_deposit_embed(base_amount, transfer_amount, order_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        try:
            msg = await interaction.original_response()
            orders[order_id]["ephemeral"] = {
                "application_id": interaction.application_id,
                "token": interaction.token,
                "message_id": msg.id,
            }
            _save_data()
        except Exception as e:
            log.warning("Khong luu ephemeral nap tien: %s", e)

class BuyModal(discord.ui.Modal):
    qty_input = discord.ui.TextInput(
        label="Số lượng key muốn mua",
        placeholder="Ví dụ: 1",
        max_length=2,
        default="1",
    )

    def __init__(self, pkg_id: str):
        pkg = PKG[pkg_id]
        super().__init__(title="🛒  " + pkg["name"])
        self.pkg_id = pkg_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = max(1, int(self.qty_input.value.strip()))
        except ValueError:
            return await interaction.response.send_message("❌ Số lượng không hợp lệ.", ephemeral=True)

        pkg   = PKG[self.pkg_id]
        total = pkg["price"] * qty
        uid   = interaction.user.id
        bal   = get_balance(uid)

        if bal < total:
            return await interaction.response.send_message(
                "❌ **Số dư không đủ!**\n"
                + "💰 Số dư: **" + "{:,}".format(bal) + " VNĐ**\n"
                + "💸 Cần: **" + "{:,}".format(total) + " VNĐ**\n"
                + "🔻 Thiếu: **" + "{:,}".format(total - bal) + " VNĐ**",
                ephemeral=True,
            )

        stock = count_keys(self.pkg_id)
        if stock < qty:
            return await interaction.response.send_message(
                "❌ **Hết key cho gói này!**\n"
                + "📦 Còn lại: **" + str(stock) + "** key · Bạn cần: **" + str(qty) + "**\n"
                + "Liên hệ admin để nạp thêm key vào kho.",
                ephemeral=True,
            )

        pv = PRODUCTS[pkg["product_key"]]
        await interaction.response.defer(ephemeral=True)
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="⏳  Đang lấy key từ kho...",
                description="Gói **" + pkg["name"] + "** · `" + str(qty) + "` key",
                color=pv["accent"],
            )
        )

        keys_ok: list[str] = []
        for _ in range(qty):
            k = await take_key(self.pkg_id)
            if k:
                keys_ok.append(k)
            else:
                break

        if len(keys_ok) < qty:
            if keys_ok:
                await restore_keys(self.pkg_id, keys_ok)
            return await interaction.edit_original_response(
                embed=discord.Embed(
                    title="❌  Kho key thay đổi",
                    description=(
                        "Chỉ lấy được **" + str(len(keys_ok)) + "/" + str(qty) + "** key.\n"
                        "Không trừ tiền — thử lại sau khi admin nạp kho."
                    ),
                    color=0xE74C3C,
                ),
                view=None,
            )

        deduct_balance(uid, total)

        new_bal = get_balance(uid)
        accent = pv["accent"]
        receipt = discord.Embed(title="◈  Giao dịch hoàn tất", color=accent)
        receipt.description = (
            "**" + pkg["name"] + "**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⏱️ `" + pkg["duration"] + "`  ·  🔢 `" + str(len(keys_ok)) + "` key\n"
            "💸 −`" + _fmt_vnd(pkg["price"] * len(keys_ok)) + "`  ·  💰 ví `" + _fmt_vnd(new_bal) + "`\n"
            "📦 Còn trong kho: **" + str(count_keys(self.pkg_id)) + "** key"
        )
        await interaction.edit_original_response(embed=receipt, view=None)

        if keys_ok:
            try:
                user = await bot.fetch_user(uid)
                keys_block = "\n".join("▸ `" + k + "`" for k in keys_ok)
                dm = discord.Embed(
                    title="◈  LICENSE · " + pv["label"].upper(),
                    color=accent,
                )
                dm.description = (
                    "```fix\n"
                    "┏━━━━━━━━ LICENSE UNLOCKED ━━━━━━━━┓\n"
                    "┃  " + pv["emoji"] + "  " + pkg["name"] + "\n"
                    "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛\n"
                    "```\n"
                    + keys_block + "\n\n"
                    "⏱️ **Hạn:** " + pkg["duration"] + "\n\n"
                    "⚠️ Không chia sẻ key · một thiết bị"
                )
                dm.set_footer(text="ducduy boutique · nexus")
                await user.send(embed=dm)
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ Bật DM để nhận key!", ephemeral=True
                )
            except Exception as e:
                log.error("DM key loi: %s", e)

# ══════════════════════════════════════════
# UI — DUCDUY BOUTIQUE V2
# ══════════════════════════════════════════

def embed_nexus() -> discord.Embed:
    ld = PRODUCTS["legit_drag"]
    ah = PRODUCTS["aimbot_head"]
    e = discord.Embed(
        title="✦ DUCDUY BOUTIQUE",
        description=(
            "```ansi\n"
            "\u001b[1;35m Shop ducduy boutique \u001b[0m\n"
            "```\n"
            "╭・⚡ **Giao Key Tự Động**\n"
            "├・💳 **Nạp Tiền Siêu Nhanh**\n"
            "├・🔐 **Key Riêng Tư Bảo Mật**\n"
            "╰・🛰️ **Hệ Thống Hoạt Động 24/7**\n\n"
            "## 🎯 LEGIT DRAG\n"
            "> " + ld["tagline"] + "\n"
            "> 💸 Từ **" + _fmt_vnd(_min_price("legit_drag")) + "**\n\n"
            "## 🔫 AIMBOT HEAD\n"
            "> " + ah["tagline"] + "\n"
            "> 💸 Từ **" + _fmt_vnd(_min_price("aimbot_head")) + "**"
        ),
        color=C_NEXUS,
    )
    e.add_field(
        name="🛒 Quy trình mua",
        value=(
            "```yaml\n"
            "Nạp tiền vào ví\n"
            "Chọn sản phẩm\n"
            "Chọn gói license\n"
            "Nhận key tự động\n"
            "```"
        ),
        inline=True,
    )
    e.add_field(
        name="📡 Hỗ trợ",
        value="```fix\n" + SUPPORT_TEXT + "\n```",
        inline=True,
    )
    e.add_field(
        name="📦 Tồn kho key",
        value=stock_summary_text(),
        inline=False,
    )
    e.add_field(
        name="✨ Ưu điểm",
        value=(
            "• Giao key ngay lập tức\n"
            "• Hệ thống ổn định\n"
            "• Nạp ví tự động\n"
            "• Hỗ trợ nhanh chóng"
        ),
        inline=False,
    )
    if bot.user and bot.user.display_avatar:
        e.set_author(name="DUCDUY BOUTIQUE", icon_url=bot.user.display_avatar.url)
    if SHOP_THUMBNAIL:
        e.set_image(url=SHOP_THUMBNAIL)
    foot_icon = bot.user.display_avatar.url if bot.user and bot.user.display_avatar else None
    e.set_footer(text="DUCDUY BOUTIQUE • HỆ THỐNG LICENSE", icon_url=foot_icon)
    return e

def embed_vault(product_key: str) -> discord.Embed:
    pv = PRODUCTS[product_key]
    package_lines = []
    for p in pv["packages"]:
        c = count_keys(p["id"])
        stock = "📦 `" + str(c) + "` key" if c else "🔴 **Hết hàng**"
        package_lines.append(
            "╭・⏳ **" + p["duration"] + "**\n├・💸 `" + _fmt_vnd(p["price"]) + "`\n╰・" + stock
        )
    e = discord.Embed(
        title=pv["emoji"] + " KHO LICENSE " + pv["label"].upper(),
        description=(
            "```ansi\n\u001b[1;36m" + pv["tagline"] + "\u001b[0m\n```\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(package_lines) + "\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "⚡ Chọn gói bên dưới để tiếp tục mua."
        ),
        color=pv["accent"],
    )
    e.add_field(
        name="🔐 Hệ thống giao key",
        value="Key lấy từ kho file · gửi tự động qua DM · mỗi key chỉ dùng một lần",
        inline=False,
    )
    e.set_footer(text="DUCDUY BOUTIQUE • " + product_key.upper())
    return e

def embed_guide() -> discord.Embed:
    e = discord.Embed(
        title="📡 HƯỚNG DẪN SỬ DỤNG",
        description=(
            "```yaml\n"
            "1. Chọn sản phẩm cần mua\n"
            "2. Chọn gói license\n"
            "3. Nạp tiền vào ví\n"
            "4. Hệ thống gửi key từ kho\n"
            "5. Nhận key qua DM\n"
            "```\n"
            "⚠️ **LƯU Ý**\n"
            "> Chuyển đúng số tiền\n"
            "> Ghi đúng mã NAP (nếu có)\n"
            "> Mỗi đơn có **số tiền CK riêng** (vd 6000→6003đ) — chuyển đúng số bot hiển thị"
        ),
        color=C_NEXUS,
    )
    e.add_field(
        name="💳 Hệ thống nạp tiền",
        value="• VietQR tự động\n• Cộng tiền tức thì\n• Hoạt động 24/7",
        inline=False,
    )
    e.set_footer(text="DUCDUY BOUTIQUE • GUIDE")
    return e

class PackageSelect(discord.ui.Select):
    def __init__(self, product_key: str):
        pv = PRODUCTS[product_key]
        opts = []
        for p in pv["packages"]:
            c = count_keys(p["id"])
            desc = ("Con " + str(c) + " key") if c else "Het hang — lien he admin"
            opts.append(
                discord.SelectOption(
                    label=p["duration"] + " • " + _fmt_vnd(p["price"]),
                    value=p["id"],
                    description=desc[:100],
                    emoji="📦" if c else "🔴",
                )
            )
        super().__init__(
            placeholder="⚡ Chọn gói license...",
            min_values=1,
            max_values=1,
            options=opts,
            row=0,
        )
        self.product_key = product_key

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BuyModal(self.values[0]))

class VaultView(discord.ui.View):
    def __init__(self, product_key: str):
        super().__init__(timeout=180)
        self.product_key = product_key
        self.add_item(PackageSelect(product_key))

    @discord.ui.button(label="Thoát", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def leave(self, interaction: discord.Interaction, _btn):
        await interaction.response.edit_message(
            content="```ansi\n\u001b[1;31mĐã đóng kho license\u001b[0m\n```",
            embed=None,
            view=None,
        )

class NexusHubView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="LEGIT DRAG", emoji="🎯", style=discord.ButtonStyle.success,
        custom_id="nexus_vault_legit", row=0,
    )
    async def open_legit(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_message(
            embed=embed_vault("legit_drag"), view=VaultView("legit_drag"), ephemeral=True,
        )

    @discord.ui.button(
        label="AIMBOT HEAD", emoji="🔫", style=discord.ButtonStyle.danger,
        custom_id="nexus_vault_aimbot", row=0,
    )
    async def open_aimbot(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_message(
            embed=embed_vault("aimbot_head"), view=VaultView("aimbot_head"), ephemeral=True,
        )

    @discord.ui.button(
        label="Nạp ví", emoji="💳", style=discord.ButtonStyle.primary,
        custom_id="nexus_wallet", row=1,
    )
    async def wallet(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_modal(DepositModal())

    @discord.ui.button(
        label="Số dư", emoji="✨", style=discord.ButtonStyle.secondary,
        custom_id="nexus_balance", row=1,
    )
    async def balance(self, interaction: discord.Interaction, _btn):
        bal = get_balance(interaction.user.id)
        e = discord.Embed(
            title="✨ VÍ CỦA BẠN",
            description="```ansi\n\u001b[1;32m" + _fmt_vnd(bal) + "\u001b[0m\n```",
            color=C_NEXUS,
        )
        e.set_footer(text="Cập nhật theo thời gian thực")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(
        label="Hướng dẫn", emoji="📡", style=discord.ButtonStyle.secondary,
        custom_id="nexus_guide", row=1,
    )
    async def guide(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_message(embed=embed_guide(), ephemeral=True)

# ══════════════════════════════════════════
# LENH
# ══════════════════════════════════════════

@bot.command(name="shop", aliases=["menu", "s"])
async def shop(ctx: commands.Context):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(embed=embed_nexus(), view=NexusHubView())

@bot.command()
@commands.has_permissions(administrator=True)
async def xacnhan(ctx: commands.Context, order_id: str):
    oid = order_id.upper()
    if oid not in orders:
        return await ctx.send("❌ Không tìm thấy đơn `" + oid + "`.", delete_after=10)
    if orders[oid].get("paid"):
        return await ctx.send("❌ Đơn `" + oid + "` đã thanh toán rồi.", delete_after=10)
    await confirm_payment(oid)
    await ctx.send("✅ Đã xác nhận đơn `" + oid + "`.", delete_after=10)

@bot.command()
@commands.has_permissions(administrator=True)
async def congcoin(ctx: commands.Context, user: discord.Member, amount: int):
    bal = add_balance(user.id, amount)
    await ctx.send(
        "✅ Cộng **" + "{:,}".format(amount) + " VNĐ** cho " + user.mention
        + ". Số dư: **" + "{:,}".format(bal) + " VNĐ**"
    )

@bot.command()
@commands.has_permissions(administrator=True)
async def doncho(ctx: commands.Context):
    pending = [
        (oid, o) for oid, o in orders.items()
        if not o.get("paid") and not _order_expired(o)
    ]
    if not pending:
        return await ctx.send("✅ Không có đơn nào đang chờ.")
    lines = []
    for oid, o in pending[:20]:
        exp = ""
        if _order_expired(o):
            exp = " *(het han)*"
        ck = _order_transfer_amount(o)
        cr = _order_credit_amount(o)
        lines.append(
            "`" + oid + "` CK `{:,}` → +`{:,}`đ — <@{}>{}".format(ck, cr, o["user_id"], exp)
        )
    e = discord.Embed(
        title="⏳ Đơn chờ (" + str(len(pending)) + ")",
        description="\n".join(lines),
        color=0xFFAA00,
    )
    await ctx.send(embed=e)

@bot.command()
@commands.has_permissions(administrator=True)
async def info(ctx: commands.Context):
    pending = len([o for o in orders.values() if not o.get("paid")])
    sepay_ok = "OK" if SEPAY_TOKEN and not _sepay_auth_failed else ("401/sai token" if _sepay_auth_failed else "chua cau hinh")
    await ctx.send(
        "✅ **" + str(bot.user) + "**\n"
        + "🌐 Webhook: `" + PUBLIC_URL.rstrip("/") + "/webhook`\n"
        + "🔌 Port: `" + str(WEBHOOK_PORT) + "`\n"
        + "🔑 SePay: `" + sepay_ok + "`\n"
        + "⏳ Đơn chờ: `" + str(pending) + "` / Tổng: `" + str(len(orders)) + "`\n"
        + "📦 Kho key: `" + str(count_keys_total()) + "` (thu muc `keys/`)\n"
        + stock_summary_text()[:1800],
        delete_after=30,
    )

@bot.command(name="keystock")
@commands.has_permissions(administrator=True)
async def keystock(ctx: commands.Context):
    e = discord.Embed(
        title="📦 Tồn kho key",
        description=stock_summary_text(),
        color=C_NEXUS,
    )
    e.set_footer(text="Them key: keys/<ma_goi>.txt — moi dong 1 key")
    await ctx.send(embed=e)

@bot.command()
@commands.has_permissions(administrator=True)
async def testkey(ctx: commands.Context, pkg_id: str = "ah_1d"):
    """Lay 1 key that (xoa khoi file) — chi de test."""
    if pkg_id not in PKG:
        return await ctx.send("❌ Ma goi khong hop le. VD: `ld_1d`, `ah_3h`", delete_after=10)
    await ctx.send("⏳ Dang lay key `" + pkg_id + "` tu kho...", delete_after=5)
    key = await take_key(pkg_id)
    if key:
        await ctx.send(
            "✅ Key (da xoa khoi file):\n`" + key + "`\nCon: **" + str(count_keys(pkg_id)) + "**",
            delete_after=60,
        )
    else:
        await ctx.send("❌ Het key trong `keys/" + pkg_id + ".txt`", delete_after=15)

@bot.command()
@commands.has_permissions(administrator=True)
async def sepaycheck(ctx: commands.Context):
    if not SEPAY_TOKEN:
        return await ctx.send("❌ SEPAY_TOKEN chua cau hinh!", delete_after=10)
    status, txn_data = await _sepay_get({"limit": 10})
    if status == 401:
        return await ctx.send(
            "❌ **SePay 401** — Token sai hoặc hết hạn.\n"
            "Vào [my.sepay.vn](https://my.sepay.vn) → API → tạo token mới → dán vào Render Environment `SEPAY_TOKEN` → Deploy lại.",
            delete_after=30,
        )
    txns  = txn_data.get("transactions", [])
    lines = ["**📥 " + str(len(txns)) + " giao dich gan nhat:**"]
    if txns:
        for txn in txns[:5]:
            amt  = int(float(txn.get("amount_in", 0) or 0))
            date = str(txn.get("transaction_date", ""))
            cont = str(txn.get("transaction_content", ""))[:40]
            lines.append("  `" + date + "` **+" + "{:,}".format(amt) + "d** — `" + cont + "`")
    else:
        lines.append("  Khong co giao dich")

    pending = [(oid, o) for oid, o in orders.items() if not o.get("paid")]
    lines.append("\n**Don cho: " + str(len(pending)) + "**")
    for oid, o in pending[:5]:
        lines.append(
            "  `" + oid + "` CK `{:,}` +`{:,}` — <@{}>".format(
                _order_transfer_amount(o), _order_credit_amount(o), o["user_id"]
            )
        )

    e = discord.Embed(title="SePay Status", description="\n".join(lines), color=0x00BFFF)
    await ctx.send(embed=e)

@bot.command()
@commands.has_permissions(administrator=True)
async def sepayreset(ctx: commands.Context):
    """Reset cờ 401 SePay sau khi đổi token."""
    global _sepay_auth_failed
    _sepay_auth_failed = False
    await ctx.send("✅ Đã reset trạng thái SePay. Thử `!sepaycheck`.")

@bot.command()
@commands.has_permissions(administrator=True)
async def debugsepay(ctx: commands.Context):
    if not SEPAY_TOKEN:
        return await ctx.send("❌ SEPAY_TOKEN chua cau hinh!", delete_after=10)
    status, data = await _sepay_get({"limit": 5})
    if status == 401:
        return await ctx.send("❌ SePay 401 — cap nhat SEPAY_TOKEN tren Render.", delete_after=15)
    txns = data.get("transactions", [])
    if not txns:
        return await ctx.send("SePay khong co giao dich.", delete_after=15)
    lines = []
    for i, txn in enumerate(txns[:5]):
        amt = int(float(txn.get("amount_in", 0) or 0))
        lines.append(
            "**[" + str(i) + "]** `" + str(txn.get("transaction_content", "N/A")) + "` "
            + "| **" + "{:,}".format(amt) + "d**"
        )
    e = discord.Embed(title="SePay Debug", description="\n".join(lines), color=0x00BFFF)
    await ctx.send(embed=e)

# ══════════════════════════════════════════
# EVENTS
# ══════════════════════════════════════════

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Bạn không có quyền dùng lệnh này.", delete_after=8)
        return
    log.error("Command error: %s", error, exc_info=error)

_webhook_started = False

@bot.event
async def on_ready():
    global _webhook_started
    log.info("Bot online: %s (ID: %d)", bot.user, bot.user.id)

    if not _webhook_started:
        try:
            await start_webhook_server()
            _webhook_started = True
        except Exception as e:
            log.error("Webhook loi: %s", e)

    if not poll_sepay.is_running():
        poll_sepay.start()

    if not SEPAY_TOKEN:
        log.warning("SEPAY_TOKEN chua cau hinh!")
    elif _sepay_auth_failed:
        log.warning("SEPAY_TOKEN bi 401 — can cap nhat tren Render")
    else:
        log.info("SEPAY_TOKEN OK (do dai %d)", len(SEPAY_TOKEN))
    log.info("Kho key: %d tong | %s", count_keys_total(), KEYS_DIR)
    for pid in PKG:
        c = count_keys(pid)
        if c:
            log.info("  %s: %d key", pid, c)

bot.run(TOKEN)
