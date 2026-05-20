import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web
import aiohttp
import asyncio
import os
import random
import json
import logging
import time
import datetime

# ══════════════════════════════════════════
# LOAD ENV
# ══════════════════════════════════════════

load_dotenv()

TOKEN          = os.getenv("DISCORD_TOKEN")
BANK_NUMBER    = os.getenv("BANK_NUMBER")
BANK_NAME      = os.getenv("BANK_NAME", "msb")
SEPAY_TOKEN    = os.getenv("SEPAY_TOKEN", "")
API_BASE       = os.getenv("API_BASE", "https://aovduy.onrender.com")
API_ADMIN_USER = os.getenv("API_ADMIN_USER", "admin")
API_ADMIN_PASS = os.getenv("API_ADMIN_PASS", "admin123")
WEBHOOK_PORT   = int(os.getenv("PORT", "8080"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shop")

# ══════════════════════════════════════════
# BOT SETUP
# ══════════════════════════════════════════

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# ══════════════════════════════════════════
# PERSISTENT STORAGE
# ══════════════════════════════════════════

DATA_FILE = "data.json"

def _load_data():
    global balances, orders
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d        = json.load(f)
            balances = {int(k): v for k, v in d.get("balances", {}).items()}
            orders   = d.get("orders", {})
            pending  = len([o for o in orders.values() if not o.get("paid")])
            log.info("Loaded %d don (%d cho), %d user", len(orders), pending, len(balances))
    except FileNotFoundError:
        log.info("Chua co data.json, bat dau moi")
    except Exception as e:
        log.error("Load data loi: %s", e)

def _save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"balances": balances, "orders": orders}, f, ensure_ascii=False)
    except Exception as e:
        log.error("Save data loi: %s", e)

# ══════════════════════════════════════════
# DATA IN-MEMORY
# ══════════════════════════════════════════

balances: dict[int, int]  = {}
orders:   dict[str, dict] = {}

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
    oid = "NAP" + str(random.randint(10000, 99999))
    while oid in orders:
        oid = "NAP" + str(random.randint(10000, 99999))
    return oid

def build_qr_url(amount: int, order_id: str) -> str:
    bank = BANK_NAME.lower().strip()
    return (
        "https://img.vietqr.io/image/" + bank + "-" + str(BANK_NUMBER) + "-compact2.png"
        + "?amount=" + str(amount)
        + "&addInfo=" + order_id
        + "&accountName=DUCDUY%20BOUTIQUE"
    )

# ══════════════════════════════════════════
# HELPER: PARSE GIAO DICH SEPAY
# Ho tro ca webhook format lan API list format
# ══════════════════════════════════════════

def _parse_amount(val) -> int:
    try:
        return int(float(val or 0))
    except (ValueError, TypeError):
        return 0

def _get_txn_amount(txn: dict) -> int:
    # Webhook SePay dung "transferAmount", API list dung "amount_in"
    val = txn.get("transferAmount") or txn.get("amount_in") or txn.get("amount") or 0
    return _parse_amount(val)

def _get_txn_text(txn: dict) -> str:
    # Gom tat ca field text de tim ma don
    parts = [
        str(txn.get("transaction_content") or ""),
        str(txn.get("content")             or ""),
        str(txn.get("description")         or ""),
        str(txn.get("code")                or ""),
        str(txn.get("reference_number")    or ""),
        str(txn.get("referenceCode")       or ""),
        str(txn.get("sub_account")         or ""),
        str(txn.get("subAccount")          or ""),
    ]
    return " ".join(parts).upper()

def _get_txn_date(txn: dict) -> str:
    return str(txn.get("transactionDate") or txn.get("transaction_date") or "")

def _match_order(txn: dict, oid: str, order: dict) -> bool:
    amount       = _get_txn_amount(txn)
    order_amount = order["amount"]
    all_text     = _get_txn_text(txn)
    txn_date_str = _get_txn_date(txn)

    log.debug(
        "  Check don %s: amount=%d/%d | oid_in_text=%s | text=%.80s",
        oid, amount, order_amount, oid.upper() in all_text, all_text
    )

    # Cach 1: tim ma don trong noi dung chuyen khoan
    if oid.upper() in all_text:
        if amount >= order_amount:
            log.info("  Khop MA DON: %s trong text", oid)
            return True
        log.debug("  Tim thay ma %s nhung amount %d < %d", oid, amount, order_amount)
        return False

    # Cach 2: khop amount chinh xac + thoi gian trong 20 phut sau tao don
    if amount != order_amount:
        return False

    order_created = order.get("created_at", 0)
    try:
        txn_ts = datetime.datetime.strptime(txn_date_str, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        txn_ts = 0

    if txn_ts > 0 and txn_ts >= order_created and (txn_ts - order_created) <= 1200:
        log.info(
            "  Khop AMOUNT+TIME: don %s %d | txn %s | tao don %s",
            oid, order_amount, txn_date_str,
            datetime.datetime.fromtimestamp(order_created).strftime("%H:%M:%S")
        )
        return True

    return False

# ══════════════════════════════════════════
# GOI BACKEND TAO KEY
# ══════════════════════════════════════════

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
            log.info("Login backend OK, tao key %d ngay cho %s", days, package_id)

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
                key_str = data.get("key")
                log.info("Tao key thanh cong: %s", key_str)
                return key_str
            else:
                log.error("Tao key that bai %d: %s", key_resp.status, data)
                return None
    except Exception as e:
        log.error("fetch_key loi: %s", e)
        return None

# ══════════════════════════════════════════
# XAC NHAN THANH TOAN
# ══════════════════════════════════════════

async def confirm_payment(order_id: str):
    order = orders.get(order_id)
    if not order or order.get("paid"):
        return
    order["paid"] = True
    _save_data()
    uid    = order["user_id"]
    amount = order["amount"]
    bal    = add_balance(uid, amount)
    log.info("Xac nhan don %s | +%d | user %d | du %d", order_id, amount, uid, bal)
    try:
        user = await bot.fetch_user(uid)
        embed = discord.Embed(title="✅  Nạp tiền thành công!", color=0x2ECC71)
        embed.description = (
            "💵 Số tiền nạp: **" + "{:,}".format(amount) + " VNĐ**\n"
            + "💰 Số dư hiện tại: **" + "{:,}".format(bal) + " VNĐ**\n"
            + "🧾 Mã đơn: `" + order_id + "`\n\n"
            + "👉 Vào shop để mua key ngay!"
        )
        embed.set_footer(text="ducduy boutique")
        await user.send(embed=embed)
    except Exception as e:
        log.warning("Khong DM duoc user %d: %s", uid, e)

# ══════════════════════════════════════════
# POLLING SEPAY (du phong, moi 15 giay)
# ══════════════════════════════════════════

@tasks.loop(seconds=15)
async def poll_sepay():
    pending = [oid for oid, o in orders.items() if not o.get("paid")]
    log.info("Poll: %d don cho: %s", len(pending), pending)

    if not pending:
        return
    if not SEPAY_TOKEN:
        log.warning("SEPAY_TOKEN chua duoc cau hinh!")
        return

    try:
        headers = {"Authorization": "Bearer " + SEPAY_TOKEN}
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://my.sepay.vn/userapi/transactions/list",
                headers=headers,
                params={"limit": 20},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    log.warning("SePay poll tra %d", r.status)
                    return
                data = await r.json()
                txns = data.get("transactions", [])
                log.info("SePay tra %d giao dich", len(txns))
                for i, txn in enumerate(txns[:5]):
                    log.debug(
                        "  TXN[%d] id=%s | content='%s' | code='%s' | amount=%s | date=%s",
                        i,
                        txn.get("id"),
                        txn.get("transaction_content", ""),
                        txn.get("code"),
                        txn.get("amount_in"),
                        txn.get("transaction_date", ""),
                    )

        matched_ids: set = set()
        for oid in list(pending):
            if orders.get(oid, {}).get("paid"):
                continue
            for txn in txns:
                tid = txn.get("id")
                if tid in matched_ids:
                    continue
                if _match_order(txn, oid, orders[oid]):
                    matched_ids.add(tid)
                    await confirm_payment(oid)
                    break

        still = [o for o in pending if not orders.get(o, {}).get("paid")]
        if still:
            log.info("Chua khop: %s", still)

    except Exception as e:
        log.error("poll_sepay loi: %s", e, exc_info=True)

# ══════════════════════════════════════════
# WEBHOOK SERVER
# ══════════════════════════════════════════

async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)

async def handle_webhook(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        log.info("Webhook nhan: %s", json.dumps(body, ensure_ascii=False)[:500])

        amount   = _get_txn_amount(body)
        all_text = _get_txn_text(body)
        log.info("Webhook parsed -> amount=%d | text=%.80s", amount, all_text)

        for oid, order in list(orders.items()):
            if order.get("paid"):
                continue
            if _match_order(body, oid, order):
                log.info("Webhook khop don %s!", oid)
                await confirm_payment(oid)
                return web.json_response({"success": True, "order": oid})

        log.info("Webhook khong khop don nao")
        return web.json_response({"success": False, "reason": "no_match"})

    except json.JSONDecodeError:
        log.warning("Webhook body khong phai JSON")
        return web.json_response({"success": False}, status=400)
    except Exception as e:
        log.error("Webhook loi: %s", e, exc_info=True)
        return web.json_response({"success": False}, status=500)

async def start_webhook_server():
    app = web.Application()
    app.router.add_route("*", "/",       handle_health)
    app.router.add_post("/webhook",      handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT).start()
    log.info("Webhook server cong %d", WEBHOOK_PORT)

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
        log.info("Tao don: %s - %d - user %d", order_id, amount, interaction.user.id)

        embed = discord.Embed(title="💳  Thông tin chuyển khoản", color=0xE91E8C)
        embed.description = (
            "```\n"
            "╔══════════════════════════════╗\n"
            + "  💰  Số tiền  :  " + "{:,}".format(amount) + " VNĐ\n"
            + "  🏦  Ngân hàng:  MSB Bank\n"
            + "  🔢  Số TK    :  " + str(BANK_NUMBER) + "\n"
            + "╚══════════════════════════════╝\n"
            + "```"
            + "📝 **Nội dung chuyển khoản:**\n"
            + "```\n" + order_id + "\n```"
            + "⚠️ Nhập **đúng** nội dung trên, không thêm bớt\n\n"
            + "📱 Quét mã QR bên dưới\n"
            + "✅ Bot tự động cộng tiền sau khi nhận giao dịch"
        )
        embed.set_image(url=build_qr_url(amount, order_id))
        embed.set_footer(text="Mã đơn: " + order_id + "  •  Hết hạn sau 15 phút")
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════
# MODAL MUA KEY
# ══════════════════════════════════════════

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

        keys_ok:  list[str] = []
        keys_err: int       = 0
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

# ══════════════════════════════════════════
# VIEWS
# ══════════════════════════════════════════

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

    @discord.ui.button(label="💳  Nạp tiền", style=discord.ButtonStyle.green,   row=0)
    async def btn_deposit(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_modal(DepositModal())

    @discord.ui.button(label="💰  Số dư",    style=discord.ButtonStyle.blurple, row=0)
    async def btn_balance(self, interaction: discord.Interaction, _btn):
        bal = get_balance(interaction.user.id)
        e = discord.Embed(
            title="💰  Số dư của bạn",
            description="**" + "{:,}".format(bal) + " VNĐ**",
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🛒  Mua Key",  style=discord.ButtonStyle.red,     row=0)
    async def btn_shop(self, interaction: discord.Interaction, _btn):
        await interaction.response.send_message(
            embed=embed_category(), view=CategoryView(), ephemeral=True
        )

# ══════════════════════════════════════════
# EMBED BUILDERS
# ══════════════════════════════════════════

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
# LENH ADMIN
# ══════════════════════════════════════════

@bot.command()
async def shop(ctx: commands.Context):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(embed=embed_shop(), view=ShopView())


@bot.command()
@commands.has_permissions(administrator=True)
async def xacnhan(ctx: commands.Context, order_id: str):
    """!xacnhan <ma_don>"""
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
    """!congcoin @user <so_tien>"""
    bal = add_balance(user.id, amount)
    await ctx.send(
        "✅ Cộng **" + "{:,}".format(amount) + " VNĐ** cho " + user.mention
        + ". Số dư: **" + "{:,}".format(bal) + " VNĐ**"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def doncho(ctx: commands.Context):
    """!doncho — xem don chua thanh toan"""
    pending = [(oid, o) for oid, o in orders.items() if not o.get("paid")]
    if not pending:
        return await ctx.send("✅ Không có đơn nào đang chờ.")
    lines = [
        "`" + oid + "` — " + "{:,}".format(o["amount"]) + "đ — <@" + str(o["user_id"]) + ">"
        for oid, o in pending[:20]
    ]
    e = discord.Embed(
        title="⏳ Đơn chờ (" + str(len(pending)) + ")",
        description="\n".join(lines),
        color=0xFFAA00,
    )
    await ctx.send(embed=e)


@bot.command()
@commands.has_permissions(administrator=True)
async def info(ctx: commands.Context):
    """!info — trang thai bot"""
    pending = len([o for o in orders.values() if not o.get("paid")])
    await ctx.send(
        "✅ **" + str(bot.user) + "**\n"
        + "🌐 Webhook: `https://shopducduyboutique.onrender.com/webhook`\n"
        + "🔌 Port: `" + str(WEBHOOK_PORT) + "`\n"
        + "⏳ Đơn chờ: `" + str(pending) + "` / Tổng: `" + str(len(orders)) + "`\n"
        + "🔑 Backend: `" + API_BASE + "`",
        delete_after=30,
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def testkey(ctx: commands.Context, pkg_id: str = "ah_1d"):
    """!testkey [pkg_id]"""
    await ctx.send("⏳ Đang tạo key `" + pkg_id + "`...", delete_after=5)
    key = await fetch_key(pkg_id)
    if key:
        await ctx.send("✅ Key: `" + key + "`", delete_after=30)
    else:
        await ctx.send("❌ Tạo key thất bại — xem log", delete_after=15)


@bot.command()
@commands.has_permissions(administrator=True)
async def sepaycheck(ctx: commands.Context):
    """!sepaycheck — kiem tra sepay va don cho"""
    if not SEPAY_TOKEN:
        return await ctx.send("❌ SEPAY_TOKEN chua cau hinh!", delete_after=10)
    try:
        headers = {"Authorization": "Bearer " + SEPAY_TOKEN}
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://my.sepay.vn/userapi/transactions/list",
                headers=headers,
                params={"limit": 10},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                txn_data = await r.json() if r.status == 200 else {}

        txns  = txn_data.get("transactions", [])
        lines = ["**📥 " + str(len(txns)) + " giao dich gan nhat:**"]
        if txns:
            for txn in txns[:5]:
                amt  = int(float(txn.get("amount_in", 0) or 0))
                date = str(txn.get("transaction_date", ""))
                cont = str(txn.get("transaction_content", ""))[:40]
                lines.append("  `" + date + "` **+" + "{:,}".format(amt) + "d** — `" + cont + "`")
        else:
            lines.append("  Khong co giao dich — tai khoan chua ket noi?")

        pending = [(oid, o) for oid, o in orders.items() if not o.get("paid")]
        lines.append("\n**Don cho: " + str(len(pending)) + "**")
        for oid, o in pending[:5]:
            lines.append("  `" + oid + "` — " + "{:,}".format(o["amount"]) + "d — <@" + str(o["user_id"]) + ">")

        e = discord.Embed(title="SePay Status", description="\n".join(lines), color=0x00BFFF)
        await ctx.send(embed=e)
    except Exception as ex:
        await ctx.send("Loi: " + str(ex), delete_after=15)


@bot.command()
@commands.has_permissions(administrator=True)
async def debugsepay(ctx: commands.Context):
    """!debugsepay — xem raw 5 giao dich sepay"""
    if not SEPAY_TOKEN:
        return await ctx.send("❌ SEPAY_TOKEN chua cau hinh!", delete_after=10)
    await ctx.send("Dang goi SePay...", delete_after=5)
    try:
        headers = {"Authorization": "Bearer " + SEPAY_TOKEN}
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://my.sepay.vn/userapi/transactions/list",
                headers=headers,
                params={"limit": 5},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                data = await r.json()
                txns = data.get("transactions", [])
                if not txns:
                    return await ctx.send("SePay khong co giao dich nao.", delete_after=15)
                lines = []
                for i, txn in enumerate(txns[:5]):
                    amt = int(float(txn.get("amount_in", 0) or 0))
                    lines.append(
                        "**[" + str(i) + "]** `" + str(txn.get("transaction_content", "N/A")) + "` "
                        + "| **" + "{:,}".format(amt) + "d** "
                        + "| `" + str(txn.get("transaction_date", "N/A")) + "`"
                    )
                e = discord.Embed(
                    title="SePay Debug — 5 giao dich gan nhat",
                    description="\n".join(lines),
                    color=0x00BFFF,
                )
                await ctx.send(embed=e)
    except Exception as ex:
        await ctx.send("Loi: " + str(ex), delete_after=15)

# ══════════════════════════════════════════
# READY
# ══════════════════════════════════════════

_webhook_started = False

@bot.event
async def on_ready():
    global _webhook_started
    log.info("Bot online: %s (ID: %d)", bot.user, bot.user.id)

    if not _webhook_started:
        try:
            await start_webhook_server()
            _webhook_started = True
            log.info("Webhook OK")
        except Exception as e:
            log.error("Webhook loi: %s", e)

    try:
        if not poll_sepay.is_running():
            poll_sepay.start()
            log.info("Polling SePay OK")
    except Exception as e:
        log.error("Polling loi: %s", e)

    if not SEPAY_TOKEN:
        log.warning("SEPAY_TOKEN chua duoc cau hinh trong .env!")
    else:
        log.info("SEPAY_TOKEN OK (%s...)", SEPAY_TOKEN[:8])

# ══════════════════════════════════════════
# RUN
# ══════════════════════════════════════════

bot.run(TOKEN)
