import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web
import aiohttp
import asyncio
import os
import re
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

# Nút mua hàng — link redirect (cấu hình trong .env hoặc Render)
BUY_LEGIT_URL  = _clean_env(os.getenv("BUY_LEGIT_URL", "https://example.com/mua-legit-drag"))
BUY_AIMBOT_URL = _clean_env(os.getenv("BUY_AIMBOT_URL", "https://example.com/mua-aimbot-head"))

# Theme
C_NEXUS   = 0xF5C451
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
            d              = json.load(f)
            balances       = {int(k): v for k, v in d.get("balances", {}).items()}
            orders         = d.get("orders", {})
            processed_txns = set(str(x) for x in d.get("processed_txns", []))
            pending        = len([o for o in orders.values() if not o.get("paid")])
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
# TIỆN ÍCH
# ══════════════════════════════════════════

def get_balance(uid: int) -> int:
    return balances.get(uid, 0)

def add_balance(uid: int, amount: int) -> int:
    balances[uid] = balances.get(uid, 0) + amount
    _save_data()
    return balances[uid]

def make_order_id() -> str:
    oid = "NAP" + str(int(time.time()))
    n = 0
    while oid in orders:
        n += 1
        oid = "NAP" + str(int(time.time())) + str(n)
    return oid

def _order_expired(order: dict) -> bool:
    return (time.time() - order.get("created_at", 0)) > ORDER_EXPIRE

def _fmt_vnd(n: int) -> str:
    return "{:,}".format(n) + "₫"

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
        color=C_NEXUS,
    )
    e.add_field(
        name="💵  Thông tin nạp",
        value=(
            "**Số tiền cần nạp:** `" + "{:,}".format(amount) + " VNĐ`\n"
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

# ══════════════════════════════════════════
# PARSE TXN — SEPAY / MSB
# ══════════════════════════════════════════

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
    t = txn.get("transferType")
    if t is not None:
        return str(t).lower() == "in"
    try:
        ain  = float(txn.get("amount_in")  or 0)
        aout = float(txn.get("amount_out") or 0)
        return ain > 0 and aout == 0
    except (TypeError, ValueError):
        return False

def _pending_same_amount(amount: int) -> list[str]:
    return [
        oid
        for oid, o in orders.items()
        if not o.get("paid") and o.get("amount") == amount and not _order_expired(o)
    ]

def _txn_matches_order(txn: dict, oid: str, order: dict) -> bool:
    amount       = _get_txn_amount(txn)
    order_amount = order.get("amount", 0)
    all_text     = _get_txn_text(txn)

    # 1) Mã NAP trong nội dung CK
    if _order_id_in_text(oid, all_text):
        if amount >= order_amount:
            log.info("Khop MA DON %s | %d>=%d | %.50s", oid, amount, order_amount, all_text)
            return True
        log.warning("Ma don %s trong CK nhung thieu tien: %d < %d", oid, amount, order_amount)
        return False

    # 2) Đúng số tiền (MSB/NAPAS không gửi mã NAP trong content)
    if amount != order_amount:
        return False

    same = _pending_same_amount(amount)
    if len(same) == 1 and oid == same[0]:
        log.info("Khop AMOUNT (1 don cho) %s | %d | sepay_text=%.50s", oid, order_amount, all_text)
        return True

    order_created = order.get("created_at", 0)
    txn_ts = _txn_timestamp(txn, order_created)
    if oid in same and txn_ts >= (order_created - 300) and (txn_ts - order_created) <= 3600:
        log.info("Khop AMOUNT+TIME don %s | %d VND", oid, order_amount)
        return True

    return False

def _find_order_for_txn(txn: dict) -> tuple[str | None, str | None]:
    fp = _txn_fingerprint(txn)
    if fp in processed_txns:
        return None, None
    if not _is_incoming(txn):
        return None, None
    amount = _get_txn_amount(txn)
    if amount <= 0:
        return None, None

    for oid, order in sorted(
        orders.items(),
        key=lambda x: x[1].get("created_at", 0),
        reverse=True,
    ):
        if order.get("paid") or _order_expired(order):
            continue
        if _txn_matches_order(txn, oid, order):
            return oid, fp

    return None, None

# ══════════════════════════════════════════
# SEPAY API
# ══════════════════════════════════════════

async def _sepay_get(params: dict | None = None) -> tuple[int, dict]:
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
                    log.error("SePay HTTP 401 — SEPAY_TOKEN sai hoac het han. Body: %s", body[:200])
                    return 401, {}
                if r.status != 200:
                    log.warning("SePay HTTP %s: %s", r.status, body[:200])
                    return r.status, {}
                _sepay_auth_failed = False
                try:
                    return r.status, json.loads(body)
                except json.JSONDecodeError:
                    return r.status, {}
    except Exception as e:
        log.error("SePay request loi: %s", e)
        return 0, {}

# ══════════════════════════════════════════
# XÁC NHẬN THANH TOÁN
# ══════════════════════════════════════════

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
            value="Quay lại shop → nhấn **🛒 Mua hàng** để chọn sản phẩm.",
            inline=False,
        )
        embed.set_footer(text="ducduy boutique")
        await user.send(embed=embed)
    except Exception as e:
        log.warning("Khong DM duoc user %s: %s", uid, e)

    await _replace_deposit_message(order_id, amount, bal)

def _deposit_success_embed(amount: int, balance: int) -> discord.Embed:
    e = discord.Embed(
        title="✅  Nạp tiền thành công!",
        description=(
            "Hệ thống đã cộng tiền vào ví của bạn.\n\n"
            "📩 **Vui lòng check DM** để xem chi tiết giao dịch.\n"
            "🛒 Nhấn **Mua hàng** để chọn sản phẩm."
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

# ══════════════════════════════════════════
# POLL SEPAY
# ══════════════════════════════════════════

@tasks.loop(seconds=30)
async def poll_sepay():
    pending = [oid for oid, o in orders.items() if not o.get("paid") and not _order_expired(o)]
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
            pending, _get_txn_amount(t0), _get_txn_text(t0),
        )

# ══════════════════════════════════════════
# WEBHOOK SERVER
# ══════════════════════════════════════════

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
        amt  = _get_txn_amount(body)
        text = _get_txn_text(body)
        log.info("Webhook: id=%s amount=%s type=%s | content=%.80s",
                 body.get("id"), amt, body.get("transferType"), text)

        oid, fp = _find_order_for_txn(body)
        if oid:
            log.info("Webhook khop don %s", oid)
            await confirm_payment(oid, fp)
            return web.json_response({"success": True})

        pending = [o for o, ord in orders.items() if not ord.get("paid") and not _order_expired(ord)]
        if pending:
            log.warning("Webhook KHONG KHOP | amount=%s | pending=%s | text=%.100s", amt, pending, text)

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
# MODAL NẠP TIỀN
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

        if not BANK_NUMBER:
            return await interaction.response.send_message(
                "❌ Bot chưa cấu hình `BANK_NUMBER`. Liên hệ admin.", ephemeral=True
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

        embed = build_deposit_embed(amount, order_id)
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

# ══════════════════════════════════════════
# EMBED & VIEW
# ══════════════════════════════════════════

def embed_nexus() -> discord.Embed:
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
            "> Ghim Ngực - Kéo Tâm Dễ Dàng - Phù Hợp Chơi Chay\n\n"
            "## 🔫 AIMBOT HEAD\n"
            "> Ghim Đầu Chặt - Không Lỗi Dame - Dễ Sử Dụng"
        ),
        color=C_NEXUS,
    )
    e.add_field(
        name="🛒 Quy trình mua",
        value=(
            "```yaml\n"
            "1. Nạp tiền vào ví\n"
            "2. Nhấn Mua hàng\n"
            "3. Chọn sản phẩm & gói\n"
            "4. Nhận key qua DM\n"
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

def embed_guide() -> discord.Embed:
    e = discord.Embed(
        title="📡 HƯỚNG DẪN SỬ DỤNG",
        description=(
            "```yaml\n"
            "1. Nhấn Nạp ví → nhập số tiền\n"
            "2. Chuyển khoản đúng số tiền + nội dung\n"
            "3. Bot tự cộng tiền sau ~1-2 phút\n"
            "4. Nhấn Mua hàng → chọn sản phẩm\n"
            "5. Nhận key qua DM\n"
            "```\n"
            "⚠️ **LƯU Ý**\n"
            "> Chuyển **đúng số tiền**\n"
            "> Ghi **đúng nội dung** (mã NAP...)\n"
            "> MSB có thể không hiện mã — bot vẫn khớp theo số tiền\n"
            "> Bật DM để nhận key"
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

class NexusHubView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Nút mua hàng dạng link (không cần bot xử lý key)
        self.add_item(discord.ui.Button(
            label="🎯 MUA LEGIT DRAG",
            url=BUY_LEGIT_URL,
            row=0,
        ))
        self.add_item(discord.ui.Button(
            label="🔫 MUA AIMBOT HEAD",
            url=BUY_AIMBOT_URL,
            row=0,
        ))

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
# LỆNH
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
        lines.append(
            "`" + oid + "` — " + "{:,}".format(o["amount"]) + "đ — <@" + str(o["user_id"]) + ">"
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
    sepay_ok = (
        "OK" if SEPAY_TOKEN and not _sepay_auth_failed
        else ("401/sai token" if _sepay_auth_failed else "chua cau hinh")
    )
    await ctx.send(
        "✅ **" + str(bot.user) + "**\n"
        + "🌐 Webhook: `" + PUBLIC_URL.rstrip("/") + "/webhook`\n"
        + "🔌 Port: `" + str(WEBHOOK_PORT) + "`\n"
        + "🔑 SePay: `" + sepay_ok + "`\n"
        + "⏳ Đơn chờ: `" + str(pending) + "` / Tổng: `" + str(len(orders)) + "`",
        delete_after=30,
    )

@bot.command()
@commands.has_permissions(administrator=True)
async def sepaycheck(ctx: commands.Context):
    if not SEPAY_TOKEN:
        return await ctx.send("❌ SEPAY_TOKEN chua cau hinh!", delete_after=10)
    status, txn_data = await _sepay_get({"limit": 10})
    if status == 401:
        return await ctx.send(
            "❌ **SePay 401** — Token sai hoặc hết hạn.\n"
            "Vào [my.sepay.vn](https://my.sepay.vn) → API → tạo token mới → dán vào Render `SEPAY_TOKEN` → Deploy lại.",
            delete_after=30,
        )
    txns  = txn_data.get("transactions", [])
    lines = ["**📥 " + str(len(txns)) + " giao dich gan nhat:**"]
    for txn in txns[:5]:
        amt  = int(float(txn.get("amount_in", 0) or 0))
        date = str(txn.get("transaction_date", ""))
        cont = str(txn.get("transaction_content", ""))[:40]
        lines.append("  `" + date + "` **+" + "{:,}".format(amt) + "đ** — `" + cont + "`")
    if not txns:
        lines.append("  Khong co giao dich")

    pending = [(oid, o) for oid, o in orders.items() if not o.get("paid")]
    lines.append("\n**Don cho: " + str(len(pending)) + "**")
    for oid, o in pending[:5]:
        lines.append("  `" + oid + "` — " + "{:,}".format(o["amount"]) + "đ")

    e = discord.Embed(title="SePay Status", description="\n".join(lines), color=0x00BFFF)
    await ctx.send(embed=e)

@bot.command()
@commands.has_permissions(administrator=True)
async def sepayreset(ctx: commands.Context):
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
            + "| **" + "{:,}".format(amt) + "đ**"
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

@bot.event
async def on_ready():
    log.info("Bot online: %s (ID: %d)", bot.user, bot.user.id)
    if not poll_sepay.is_running():
        poll_sepay.start()
    if not SEPAY_TOKEN:
        log.warning("SEPAY_TOKEN chua cau hinh!")
    elif _sepay_auth_failed:
        log.warning("SEPAY_TOKEN bi 401 — can cap nhat tren Render")
    else:
        log.info("SEPAY_TOKEN OK (do dai %d)", len(SEPAY_TOKEN))

# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

async def main():
    await start_webhook_server()
    await bot.start(TOKEN)

asyncio.run(main())
