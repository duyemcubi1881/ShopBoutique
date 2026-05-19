import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web
import aiohttp
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
API_BASE       = _clean_env(os.getenv("API_BASE", "https://aovduy.onrender.com"))
API_ADMIN_USER = _clean_env(os.getenv("API_ADMIN_USER", "admin"))
API_ADMIN_PASS = _clean_env(os.getenv("API_ADMIN_PASS", "admin123"))
PUBLIC_URL     = _clean_env(
    os.getenv("PUBLIC_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://shopboutique.onrender.com"
)
WEBHOOK_PORT   = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT") or "8080")

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
        PKG[_pkg["id"]] = {**_pkg, "product_label": _pv["label"]}

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

def build_deposit_embed(amount: int, order_id: str) -> discord.Embed:
    e = discord.Embed(
        title="💳  Thông tin nạp tiền",
        description="Chuyển khoản **đúng** thông tin bên dưới — bot sẽ **tự động cộng tiền** khi nhận được.",
        color=0xE91E8C,
    )
    e.add_field(
        name="💵  Thông tin nạp",
        value=(
            "**Số tiền cần nạp:** `" + "{:,}".format(amount) + " VNĐ`\n"
            + "**Mã đơn hàng:** `" + order_id + "`\n"
            + "**Nội dung chuyển khoản:** `" + order_id + "`"
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
            + "2️⃣  Nhập **đúng số tiền:** `" + "{:,}".format(amount) + " VNĐ`\n"
            + "3️⃣  Nhập **đúng nội dung:** `" + order_id + "` — không thêm bớt ký tự\n"
            + "4️⃣  Hệ thống **tự cộng tiền** trong ~1–2 phút sau khi ngân hàng ghi nhận"
        ),
        inline=False,
    )
    e.set_image(url=build_qr_url(amount, order_id))
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

def _txn_timestamp(txn: dict) -> float:
    s = _get_txn_date(txn)
    if not s:
        return time.time()
    try:
        dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=VN_TZ)
        return dt.timestamp()
    except Exception:
        return time.time()

def _txn_fingerprint(txn: dict) -> str:
    tid = str(txn.get("id") or "").strip()
    if tid and tid not in ("0", "None"):
        return "id:" + tid
    ref = str(txn.get("referenceCode") or txn.get("reference_number") or "").strip()
    if ref:
        return "ref:" + ref
    return "fp:" + _get_txn_date(txn) + "|" + str(_get_txn_amount(txn)) + "|" + _get_txn_text(txn)[:80]

def _is_incoming(txn: dict) -> bool:
    t = txn.get("transferType")
    if t is not None:
        return str(t).lower() == "in"
    try:
        return float(txn.get("amount_in") or 0) > 0
    except (TypeError, ValueError):
        return False

def _pending_same_amount(amount: int) -> list[str]:
    return [
        oid
        for oid, o in orders.items()
        if not o.get("paid") and o.get("amount") == amount and not _order_expired(o)
    ]

def _match_order(txn: dict, oid: str, order: dict) -> bool:
    if order.get("paid") or _order_expired(order):
        return False
    if not _is_incoming(txn):
        return False

    amount       = _get_txn_amount(txn)
    order_amount = order["amount"]
    if amount <= 0:
        return False

    all_text     = _get_txn_text(txn)
    txn_date_str = _get_txn_date(txn)

    # Ưu tiên: mã đơn trong nội dung CK (fuzzy) + đủ số tiền
    if _order_id_in_text(oid, all_text):
        if amount >= order_amount:
            log.info("Khop MA DON %s | amount %d >= %d | text=%.60s", oid, amount, order_amount, all_text)
            return True
        log.warning("Ma don %s trong CK nhung thieu tien: %d < %d", oid, amount, order_amount)
        return False

    # Dự phòng: đúng số tiền + đơn tạo trước giao dịch (tối đa 30 phút)
    if amount != order_amount:
        return False

    order_created = order.get("created_at", 0)
    txn_ts = _txn_timestamp(txn)
    if txn_ts >= (order_created - 60) and (txn_ts - order_created) <= 1800:
        same = _pending_same_amount(amount)
        if oid in same:
            log.info("Khop AMOUNT+TIME don %s | %d VND", oid, order_amount)
            return True

    return False

def _find_order_for_txn(txn: dict) -> tuple[str | None, str | None]:
    """Tra don khop; tra (order_id, fingerprint)."""
    fp = _txn_fingerprint(txn)
    if fp in processed_txns:
        return None, None

    # Ưu tiên đơn có mã trong nội dung CK
    text = _get_txn_text(txn)
    amount = _get_txn_amount(txn)
    for oid, order in sorted(
        orders.items(),
        key=lambda x: x[1].get("created_at", 0),
        reverse=True,
    ):
        if order.get("paid") or _order_expired(order):
            continue
        if _order_id_in_text(oid, text) and amount >= order.get("amount", 0):
            if _match_order(txn, oid, order):
                return oid, fp

    for oid, order in list(orders.items()):
        if _match_order(txn, oid, order):
            return oid, fp

    return None, None

async def _sepay_get(params: dict | None = None) -> tuple[int, dict]:
    """Goi SePay API; tra (status, json)."""
    global _sepay_auth_failed
    if not SEPAY_TOKEN:
        return 0, {}
    if _sepay_auth_failed:
        return 401, {}

    headers = {"Authorization": "Bearer " + SEPAY_TOKEN}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://my.sepay.vn/userapi/transactions/list",
                headers=headers,
                params=params or {"limit": 20},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                body = await r.text()
                if r.status == 401:
                    _sepay_auth_failed = True
                    log.error(
                        "SePay HTTP 401 — SEPAY_TOKEN sai hoac het han. "
                        "Vao my.sepay.vn -> API -> tao token moi -> cap nhat Environment tren Render. Body: %s",
                        body[:200],
                    )
                    return 401, {}
                if r.status != 200:
                    log.warning("SePay HTTP %s: %s", r.status, body[:200])
                    return r.status, {}
                try:
                    return r.status, json.loads(body)
                except json.JSONDecodeError:
                    return r.status, {}
    except Exception as e:
        log.error("SePay request loi: %s", e)
        return 0, {}

async def fetch_key(package_id: str) -> str | None:
    pkg  = PKG.get(package_id)
    days = pkg["days"] if pkg else 1
    try:
        async with aiohttp.ClientSession() as s:
            login_resp = await s.post(
                API_BASE + "/api/login",
                json={"username": API_ADMIN_USER, "password": API_ADMIN_PASS},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if login_resp.status != 200:
                body = await login_resp.text()
                log.error("Login backend that bai %d: %s", login_resp.status, body[:200])
                return None
            key_resp = await s.post(
                API_BASE + "/api/createkey",
                json={
                    "days": days,
                    "key_type": "single_device",
                    "created_by": "ShopBot",
                    "note": "Auto-" + package_id,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await key_resp.json()
            if key_resp.status == 201:
                return data.get("key")
            log.error("Tao key that bai %d: %s", key_resp.status, data)
            return None
    except Exception as e:
        log.error("fetch_key loi: %s", e)
        return None

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

    amount = order["amount"]
    bal    = add_balance(uid, amount)
    log.info("XAC NHAN %s | +%s | user %s | du %s", order_id, amount, uid, bal)

    try:
        user = await bot.fetch_user(uid)
        embed = discord.Embed(
            title="✅  Nạp tiền thành công!",
            description="Giao dịch đã được xác nhận tự động.",
            color=0x2ECC71,
        )
        embed.add_field(name="💵  Đã nạp", value="`" + "{:,}".format(amount) + " VNĐ`", inline=True)
        embed.add_field(name="💰  Số dư", value="`" + "{:,}".format(bal) + " VNĐ`", inline=True)
        embed.add_field(name="🧾  Mã đơn", value="`" + order_id + "`", inline=False)
        embed.add_field(
            name="👉  Tiếp theo",
            value="Quay lại shop → **🛒 Mua Key** để nhận key qua DM.",
            inline=False,
        )
        embed.set_footer(text="ducduy boutique")
        await user.send(embed=embed)
    except Exception as e:
        log.warning("Khong DM duoc user %s: %s", uid, e)

@tasks.loop(seconds=10)
async def poll_sepay():
    pending = [
        oid for oid, o in orders.items()
        if not o.get("paid") and not _order_expired(o)
    ]
    if not pending:
        return
    if not SEPAY_TOKEN or _sepay_auth_failed:
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
        log.info(
            "Poll chua khop | pending=%s | txn moi amount=%s text=%.60s",
            pending,
            _get_txn_amount(t0),
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

        order_id = make_order_id()
        orders[order_id] = {
            "user_id":    interaction.user.id,
            "amount":     amount,
            "paid":       False,
            "created_at": time.time(),
        }
        _save_data()
        log.info("Tao don: %s | %s VND | user %s", order_id, amount, interaction.user.id)

        if not BANK_NUMBER:
            return await interaction.response.send_message(
                "❌ Bot chưa cấu hình `BANK_NUMBER` trên server. Liên hệ admin.",
                ephemeral=True,
            )

        embed = build_deposit_embed(amount, order_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        await interaction.response.defer(ephemeral=True)
        deduct_balance(uid, total)

        keys_ok: list[str] = []
        keys_err = 0
        for _ in range(qty):
            k = await fetch_key(self.pkg_id)
            if k:
                keys_ok.append(k)
            else:
                keys_err += 1

        if keys_err:
            add_balance(uid, pkg["price"] * keys_err)

        new_bal = get_balance(uid)
        embed = discord.Embed(title="✅  Mua key thành công!", color=0x2ECC71)
        embed.description = (
            "🛒 **" + pkg["name"] + "**\n"
            + "⏱️ Thời hạn: **" + pkg["duration"] + "**\n"
            + "🔢 Số lượng: **" + str(len(keys_ok)) + " key**\n"
            + "💸 Đã trừ: **" + "{:,}".format(pkg["price"] * len(keys_ok)) + " VNĐ**\n"
            + "💰 Số dư còn: **" + "{:,}".format(new_bal) + " VNĐ**"
        )
        if keys_err:
            embed.add_field(
                name="⚠️ Lưu ý",
                value=str(keys_err) + " key lỗi → đã hoàn **" + "{:,}".format(pkg["price"] * keys_err) + " VNĐ**",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

        if keys_ok:
            try:
                user     = await bot.fetch_user(uid)
                key_text = "\n".join("`" + k + "`" for k in keys_ok)
                dm = discord.Embed(title="🔑  Key của bạn!", color=0xE91E8C)
                dm.description = (
                    "```\n"
                    "╔══════════════════════════════╗\n"
                    "       ✅  Mua thành công\n"
                    "╚══════════════════════════════╝\n"
                    "```"
                    + "🛒 **" + pkg["name"] + "**\n"
                    + "⏱️ Thời hạn: **" + pkg["duration"] + "**\n\n"
                    + "🔑 **Key:**\n" + key_text + "\n\n"
                    + "📁 File & hướng dẫn trong server\n"
                    + "🙏 Cảm ơn bạn đã dùng **ducduy boutique**"
                )
                dm.set_footer(text="⚠️ Không chia sẻ key với người khác!")
                await user.send(embed=dm)
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ Không gửi DM được. Hãy mở DM để nhận key!", ephemeral=True
                )
            except Exception as e:
                log.error("DM key loi: %s", e)

class PackageButton(discord.ui.Button):
    def __init__(self, pkg: dict):
        super().__init__(
            label=pkg["name"] + "  —  " + "{:,}".format(pkg["price"]) + "đ",
            style=discord.ButtonStyle.primary,
            custom_id="pkg_" + pkg["id"],
        )
        self.pkg_id = pkg["id"]

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BuyModal(self.pkg_id))

class PackageView(discord.ui.View):
    def __init__(self, product_key: str):
        super().__init__(timeout=120)
        for pkg in PRODUCTS[product_key]["packages"]:
            self.add_item(PackageButton(pkg))

    @discord.ui.button(label="◀  Quay lại", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, _btn):
        await interaction.response.edit_message(embed=embed_category(), view=CategoryView())

class CategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="🎮  Chọn sản phẩm...",
        options=[
            discord.SelectOption(label="Legit Drag",  value="legit_drag",  emoji="🎯", description="Tu 3.000d"),
            discord.SelectOption(label="Aimbot Head", value="aimbot_head", emoji="🔫", description="Tu 5.000d"),
        ],
    )
    async def select_product(self, interaction: discord.Interaction, select: discord.ui.Select):
        pk = select.values[0]
        await interaction.response.edit_message(embed=embed_packages(pk), view=PackageView(pk))

    @discord.ui.button(label="◀  Quay lại", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _btn):
        await interaction.response.edit_message(embed=embed_shop(), view=ShopView())

class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💳  Nạp tiền", style=discord.ButtonStyle.green, row=0)
    async def btn_deposit(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_modal(DepositModal())

    @discord.ui.button(label="💰  Số dư", style=discord.ButtonStyle.blurple, row=0)
    async def btn_balance(self, interaction: discord.Interaction, _btn):
        bal = get_balance(interaction.user.id)
        e = discord.Embed(
            title="💰  Số dư của bạn",
            description="**" + "{:,}".format(bal) + " VNĐ**",
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🛒  Mua Key", style=discord.ButtonStyle.red, row=0)
    async def btn_shop(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_message(
            embed=embed_category(), view=CategoryView(), ephemeral=True
        )

def embed_shop() -> discord.Embed:
    e = discord.Embed(title="🛍️  Shop Key Tự Động — ducduy boutique", color=0xE91E8C)
    e.description = (
        "```\n"
        "╔══════════════════════════════╗\n"
        "    🔥  SAN PHAM DANG BAN\n"
        "╠══════════════════════════════╣\n"
        "  🎯 Legit Drag  |  🔫 Aimbot Head\n"
        "  💰 Tu 3,000d   |  💰 Tu 5,000d\n"
        "╠══════════════════════════════╣\n"
        "  📦 Nhan key qua DM tuc thi\n"
        "  ⚡ VietQR - cong tien tu dong\n"
        "╠══════════════════════════════╣\n"
        "    💬  SUPPORT\n"
        "  📩 DM: @CubiShop\n"
        "╚══════════════════════════════╝\n"
        "```"
    )
    e.set_footer(text="ducduy boutique  •  Chon chuc nang ben duoi")
    return e

def embed_category() -> discord.Embed:
    e = discord.Embed(title="🛒  Danh mục sản phẩm", color=0xFFD700)
    lines = []
    for pv in PRODUCTS.values():
        lines.append(pv["emoji"] + " **" + pv["label"] + "**")
        for pkg in pv["packages"]:
            lines.append("　└ " + pkg["name"] + " — **" + "{:,}".format(pkg["price"]) + "đ**")
    e.description = "\n".join(lines) + "\n\n*Chọn sản phẩm trong menu bên dưới ↓*"
    return e

def embed_packages(product_key: str) -> discord.Embed:
    pv = PRODUCTS[product_key]
    e  = discord.Embed(title=pv["emoji"] + "  " + pv["label"] + " — Chọn gói", color=0x00BFFF)
    lines = [
        "• **" + pkg["name"] + "** — " + "{:,}".format(pkg["price"]) + "đ"
        for pkg in pv["packages"]
    ]
    e.description = "\n".join(lines) + "\n\n*Ấn nút bên dưới để mua ↓*"
    return e

# ══════════════════════════════════════════
# LENH
# ══════════════════════════════════════════

@bot.command(name="shop", aliases=["menu", "s"])
async def shop(ctx: commands.Context):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(embed=embed_shop(), view=ShopView())

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
        lines.append(
            "`" + oid + "` — " + "{:,}".format(o["amount"]) + "đ — <@" + str(o["user_id"]) + ">" + exp
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
        + "🖥️ Backend: `" + API_BASE + "`",
        delete_after=30,
    )

@bot.command()
@commands.has_permissions(administrator=True)
async def testkey(ctx: commands.Context, pkg_id: str = "ah_1d"):
    await ctx.send("⏳ Đang tạo key `" + pkg_id + "`...", delete_after=5)
    key = await fetch_key(pkg_id)
    if key:
        await ctx.send("✅ Key: `" + key + "`", delete_after=30)
    else:
        await ctx.send("❌ Tạo key thất bại — xem log", delete_after=15)

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
        lines.append("  `" + oid + "` — " + "{:,}".format(o["amount"]) + "d")

    e = discord.Embed(title="SePay Status", description="\n".join(lines), color=0x00BFFF)
    await ctx.send(embed=e)

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

bot.run(TOKEN)
