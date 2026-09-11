#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kairozen SMM Shop Bot (product for buyers)
─────────────────────────────────────────
1. បំពេញ BOT_TOKEN (+ ADMIN_ID) → bot រត់
2. Admin បំពេញក្នុង bot: ឈ្មោះ Shop, Bakong ID, ឈ្មោះគណនី
3. បើគ្មាន Bakong → Admin upload QR ធម្មតា (រូប) សម្រាប់ user ដាក់លុយ
4. Order SMM តាម website API (PANEL_URL + PANEL_API_KEY)

Env ចាំបាច់:
  BOT_TOKEN     ពី @BotFather
  ADMIN_ID      Telegram numeric id

Env ស្រេចចិត្ត:
  PANEL_URL     default https://kairozen-smm.onrender.com
  PANEL_API_KEY API key ពី website
  MIN_DEPOSIT   default 0.75

Bakong KHQR (បង្កើត QR ស្វ័យប្រវត្តិ ពេល «ដាក់លុយ»):
  pip install "bakong-khqr[image]" qrcode
  Admin កំណត់ក្នុង ⚙️ Setup: Bakong ID, ឈ្មោះ, ទីក្រុង, Currency
  ស្រេចចិត្ត: Bakong Developer Token (https://api-bakong.nbc.gov.kh/register/)
    → បើមាន Token, ការបង់ប្រាក់នឹងត្រូវផ្ទៀងផ្ទាត់ស្វ័យប្រវត្តិ (auto-verify)
    → បើគ្មាន Token, QR នៅតែបង្កើតបាន ប៉ុន្តែ Admin ត្រូវ Confirm ដោយដៃ
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import requests
import telebot
from telebot import types

try:
    from bakong_khqr import KHQR  # pip install "bakong-khqr[image]"
except Exception:
    KHQR = None

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kairozen-shop")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PANEL_URL = (os.getenv("PANEL_URL") or "https://kairozen-smm.onrender.com").rstrip("/")
PANEL_API_KEY = os.getenv("PANEL_API_KEY", "").strip()
MIN_DEPOSIT = float(os.getenv("MIN_DEPOSIT", "0.75"))
DATA_DIR = Path(os.getenv("BOT_DATA_DIR") or Path(__file__).resolve().parent)
DB_FILE = DATA_DIR / "shop_bot_db.json"
QR_FILE = DATA_DIR / "payment_qr.jpg"

if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN (ពី @BotFather)")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
waiting: dict[int, str] = {}
_lock = threading.Lock()

DEFAULT_CFG = {
    "shop_name": "My SMM Shop",
    "bakong_id": "",
    "bakong_name": "",
    "bakong_city": "Phnom Penh",
    "bakong_token": "",
    "currency": "USD",
    "panel_url": PANEL_URL,
    "panel_api_key": PANEL_API_KEY,
    "min_deposit": MIN_DEPOSIT,
    "support_text": "ទាក់ទង Admin សម្រាប់ជំនួយ",
    "qr_caption": "ស្កេន QR ដើម្បីបង់លុយ បន្ទាប់មកចុច «ខ្ញុំបង់រួច»",
}


# ── Bakong KHQR ─────────────────────────────────────────────────
def generate_khqr(amount: float, bill_number: str):
    """Build a live Bakong KHQR (with amount baked in) + QR image.
    Returns (qr_string, md5_hash, png_path) or None if not configured/available.
    """
    c = cfg()
    account_id = (c.get("bakong_id") or "").strip()
    if not KHQR or not account_id:
        return None
    try:
        khqr = KHQR(c.get("bakong_token") or "no-token")
        qr_string = khqr.create_qr(
            account_id=account_id,
            merchant_name=(c.get("bakong_name") or c.get("shop_name") or "Shop")[:25],
            merchant_city=(c.get("bakong_city") or "Phnom Penh")[:15],
            amount=round(float(amount), 2),
            currency=(c.get("currency") or "USD"),
            bill_number=str(bill_number)[:25],
            store_label=(c.get("shop_name") or "Shop")[:25],
            static=False,
            expiration=1,
        )
        md5_hash = khqr.generate_md5(qr_string)
    except Exception as e:
        log.warning("KHQR create_qr failed: %s", e)
        return None
    png_path = str(DATA_DIR / f"khqr_{bill_number}.png")
    try:
        import qrcode
        qrcode.make(qr_string).save(png_path)
    except Exception as e:
        log.warning("QR image render failed: %s", e)
        png_path = None
    return qr_string, md5_hash, png_path


def check_khqr_paid(md5_hash: str) -> bool:
    c = cfg()
    if not KHQR or not md5_hash or not c.get("bakong_token"):
        return False
    try:
        khqr = KHQR(c.get("bakong_token"))
        return khqr.check_payment(md5_hash) == "PAID"
    except Exception as e:
        log.warning("check_payment failed: %s", e)
        return False


# ── DB ──────────────────────────────────────────────────────────
def _empty_db() -> dict:
    return {
        "users": {},
        "orders": {},
        "deposits": {},
        "config": dict(DEFAULT_CFG),
        "next_order": 1,
        "next_dep": 1,
    }


def _load() -> dict:
    if DB_FILE.exists():
        try:
            data = json.loads(DB_FILE.read_text(encoding="utf-8"))
            data.setdefault("config", dict(DEFAULT_CFG))
            for k, v in DEFAULT_CFG.items():
                data["config"].setdefault(k, v)
            return data
        except Exception:
            pass
    return _empty_db()


def _save(db: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_FILE)


def cfg() -> dict:
    return _load().get("config", dict(DEFAULT_CFG))


def set_cfg(**kw) -> dict:
    with _lock:
        db = _load()
        db.setdefault("config", dict(DEFAULT_CFG)).update(kw)
        _save(db)
        return db["config"]


def get_user(uid: int) -> dict:
    db = _load()
    s = str(uid)
    if s not in db["users"]:
        db["users"][s] = {
            "id": uid,
            "name": "",
            "username": "",
            "balance": 0.0,
            "created": time.time(),
        }
        _save(db)
    return db["users"][s]


def update_user(uid: int, **kw) -> dict:
    with _lock:
        db = _load()
        s = str(uid)
        u = db["users"].setdefault(
            s,
            {"id": uid, "name": "", "username": "", "balance": 0.0, "created": time.time()},
        )
        u.update(kw)
        db["users"][s] = u
        _save(db)
        return u


def add_balance(uid: int, amount: float) -> float:
    with _lock:
        db = _load()
        s = str(uid)
        u = db["users"].setdefault(
            s,
            {"id": uid, "name": "", "username": "", "balance": 0.0, "created": time.time()},
        )
        u["balance"] = round(float(u.get("balance", 0)) + amount, 4)
        db["users"][s] = u
        _save(db)
        return u["balance"]


def deduct_balance(uid: int, amount: float) -> tuple[bool, float]:
    with _lock:
        db = _load()
        s = str(uid)
        u = db["users"].get(s)
        if not u:
            return False, 0.0
        bal = float(u.get("balance", 0))
        if bal < amount:
            return False, bal
        u["balance"] = round(bal - amount, 4)
        db["users"][s] = u
        _save(db)
        return True, u["balance"]


def is_admin(uid: int) -> bool:
    return ADMIN_ID and uid == ADMIN_ID


def setup_done() -> bool:
    """Shop considered configured if name set and (bakong_id OR qr file)."""
    c = cfg()
    has_pay = bool(c.get("bakong_id")) or QR_FILE.exists()
    return bool(c.get("shop_name")) and has_pay


# ── Panel API ───────────────────────────────────────────────────
def panel(action: str, **params):
    c = cfg()
    key = (c.get("panel_api_key") or PANEL_API_KEY or "").strip()
    url = (c.get("panel_url") or PANEL_URL).rstrip("/")
    if not key:
        return {"error": "មិនទាន់កំណត់ PANEL API KEY (Admin → ⚙️ Setup)"}
    try:
        r = requests.post(
            f"{url}/api/v2",
            data={"key": key, "action": action, **params},
            timeout=45,
            headers={"User-Agent": "KairozenShopBot/2.0"},
        )
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_services() -> list:
    res = panel("services")
    return res if isinstance(res, list) else []


# ── Keyboards ───────────────────────────────────────────────────
def main_kb(uid: int) -> types.ReplyKeyboardMarkup:
    c = cfg()
    shop = c.get("shop_name") or "SMM Shop"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("🛒 បញ្ជាទិញ"), types.KeyboardButton("📦 Orders"))
    kb.add(types.KeyboardButton("💰 ដាក់លុយ"), types.KeyboardButton("👤 គណនី"))
    kb.add(types.KeyboardButton("📋 Services"), types.KeyboardButton("💬 Support"))
    if is_admin(uid):
        kb.add(types.KeyboardButton("⚙️ Setup"), types.KeyboardButton("⚙️ Admin"))
    return kb


def cancel_kb() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("❌ បោះបង់"))
    return kb


def setup_kb() -> types.InlineKeyboardMarkup:
    c = cfg()
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton(f"🏪 ឈ្មោះ Shop: {c.get('shop_name') or '—'}", callback_data="cfg:shop_name"))
    mk.add(types.InlineKeyboardButton(f"🏦 Bakong ID: {c.get('bakong_id') or '(គ្មាន)'}", callback_data="cfg:bakong_id"))
    mk.add(types.InlineKeyboardButton(f"👤 ឈ្មោះគណនី Bakong: {c.get('bakong_name') or '—'}", callback_data="cfg:bakong_name"))
    mk.add(types.InlineKeyboardButton(f"🏙 ទីក្រុង: {c.get('bakong_city') or '—'}", callback_data="cfg:bakong_city"))
    mk.add(types.InlineKeyboardButton(f"💱 Currency: {c.get('currency') or 'USD'}", callback_data="cfg:currency"))
    khqr_status = "✅ បើក (auto-verify)" if (KHQR and c.get("bakong_token")) else ("⚠️ QR ស្វ័យប្រវត្តិ (គ្មាន auto-verify)" if KHQR and c.get("bakong_id") else "❌ បិទ")
    mk.add(types.InlineKeyboardButton(f"🔐 Bakong Token: {khqr_status}", callback_data="cfg:bakong_token"))
    mk.add(types.InlineKeyboardButton("📷 Upload QR ធម្មតា (បើគ្មាន Bakong)", callback_data="cfg:upload_qr"))
    mk.add(types.InlineKeyboardButton(f"🔗 Panel URL", callback_data="cfg:panel_url"))
    mk.add(types.InlineKeyboardButton("🔑 Panel API Key", callback_data="cfg:panel_api_key"))
    mk.add(types.InlineKeyboardButton(f"💵 Min deposit: ${float(c.get('min_deposit', MIN_DEPOSIT)):.2f}", callback_data="cfg:min_deposit"))
    qr_status = "✅ មាន QR" if QR_FILE.exists() else "❌ គ្មាន QR"
    mk.add(types.InlineKeyboardButton(f"🖼 QR file: {qr_status}", callback_data="cfg:view_qr"))
    return mk


def cats_kb(services: list) -> types.InlineKeyboardMarkup:
    cats = sorted({str(s.get("category") or "Other") for s in services})
    mk = types.InlineKeyboardMarkup(row_width=2)
    btns = [types.InlineKeyboardButton(c, callback_data=f"cat:{c[:40]}") for c in cats[:20]]
    for i in range(0, len(btns), 2):
        mk.row(*btns[i : i + 2])
    return mk


def services_kb(services: list, category: str) -> types.InlineKeyboardMarkup:
    items = [s for s in services if str(s.get("category") or "Other") == category][:30]
    mk = types.InlineKeyboardMarkup(row_width=1)
    for s in items:
        sid = s.get("service") or s.get("id")
        name = str(s.get("name", "?"))[:40]
        rate = s.get("rate", "?")
        mk.add(types.InlineKeyboardButton(f"#{sid} {name} · ${rate}/1k", callback_data=f"svc:{sid}"))
    mk.add(types.InlineKeyboardButton("← ត្រឡប់", callback_data="back_cats"))
    return mk


def deposit_amt_kb() -> types.InlineKeyboardMarkup:
    c = cfg()
    mn = float(c.get("min_deposit") or MIN_DEPOSIT)
    amounts = sorted({mn, 1, 2, 5, 10, 20})
    mk = types.InlineKeyboardMarkup(row_width=3)
    row = []
    for a in amounts:
        if a < mn:
            continue
        row.append(types.InlineKeyboardButton(f"${a:g}", callback_data=f"dep:{a}"))
        if len(row) == 3:
            mk.row(*row)
            row = []
    if row:
        mk.row(*row)
    mk.add(types.InlineKeyboardButton("✏️ ចំនួនផ្សេង", callback_data="dep:custom"))
    return mk


# ── Start / menus ───────────────────────────────────────────────
@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    uid = message.from_user.id
    update_user(uid, name=message.from_user.first_name or "", username=message.from_user.username or "")
    c = cfg()
    shop = c.get("shop_name") or "SMM Shop"
    u = get_user(uid)
    text = (
        f"សួស្តី <b>{message.from_user.first_name or 'User'}</b>!\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏪 <b>{shop}</b>\n"
        f"💳 Balance: <b>${float(u.get('balance', 0)):.2f}</b>\n"
        f"💰 ដាក់លុយអប្បបរមា: <b>${float(c.get('min_deposit', MIN_DEPOSIT)):.2f}</b>\n"
    )
    if is_admin(uid) and not setup_done():
        text += (
            "\n⚠️ <b>Admin:</b> មិនទាន់ setup ទេ\n"
            "ចុច <b>⚙️ Setup</b> ដើម្បីបំពេញ:\n"
            "• ឈ្មោះ Shop\n• Bakong ID + ឈ្មោះ\n"
            "• ឬ Upload QR ធម្មតា (បើគ្មាន Bakong)\n"
            "• Panel API Key\n"
        )
    text += "\nជ្រើសមុខងារខាងក្រោម 👇"
    bot.send_message(uid, text, reply_markup=main_kb(uid))


@bot.message_handler(func=lambda m: m.text in ("❌ បោះបង់", "/cancel"))
def cmd_cancel(message: types.Message):
    waiting.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "បានបោះបង់។", reply_markup=main_kb(message.from_user.id))


@bot.message_handler(func=lambda m: m.text == "⚙️ Setup" and is_admin(m.from_user.id))
def cmd_setup(message: types.Message):
    c = cfg()
    bot.send_message(
        message.chat.id,
        f"⚙️ <b>Setup Shop</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"បំពេញព័ត៌មានខាងក្រោម។\n"
        f"• មាន <b>Bakong</b> → បំពេញ Bakong ID + ឈ្មោះ → KHQR (មានចំនួនប្រាក់) នឹងបង្កើតដោយស្វ័យប្រវត្តិរាល់ deposit\n"
        f"• បន្ថែម <b>Bakong Token</b> → ប្រព័ន្ធ auto-verify ការបង់ប្រាក់ ដោយមិនចាំបាច់ Admin ចុច Confirm\n"
        f"• <b>គ្មាន Bakong</b> → Upload QR ធម្មតា (រូប ABA/Wing/…)\n\n"
        f"Shop: <b>{c.get('shop_name')}</b>\n"
        f"Bakong: <code>{c.get('bakong_id') or '—'}</code>\n"
        f"KHQR lib: {'✅' if KHQR else '❌ (pip install \"bakong-khqr[image]\")'}\n"
        f"Auto-verify: {'✅' if c.get('bakong_token') else '❌'}\n"
        f"QR file (static): {'✅' if QR_FILE.exists() else '❌'}",
        reply_markup=setup_kb(),
    )


@bot.message_handler(func=lambda m: m.text == "⚙️ Admin" and is_admin(m.from_user.id))
def cmd_admin(message: types.Message):
    db = _load()
    c = cfg()
    bot.send_message(
        message.chat.id,
        f"⚙️ <b>Admin</b>\n"
        f"Users: {len(db.get('users', {}))}\n"
        f"Orders: {len(db.get('orders', {}))}\n"
        f"Pending deposits: {sum(1 for d in db.get('deposits', {}).values() if d.get('status')=='pending')}\n"
        f"Shop: {c.get('shop_name')}\n"
        f"Panel: {c.get('panel_url')}\n\n"
        f"/addbal user_id amount\n"
        f"/setbal user_id amount\n"
        f"/broadcast សារ...",
        reply_markup=main_kb(message.from_user.id),
    )


@bot.message_handler(commands=["addbal", "setbal"])
def cmd_admin_bal(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        bot.reply_to(message, "Usage: /addbal id amount | /setbal id amount")
        return
    try:
        tid, amt = int(parts[1]), float(parts[2])
    except ValueError:
        bot.reply_to(message, "លេខមិនត្រឹមត្រូវ")
        return
    if parts[0].startswith("/setbal"):
        update_user(tid, balance=round(amt, 4))
        bot.reply_to(message, f"Set ${amt:.4f} → {tid}")
    else:
        nb = add_balance(tid, amt)
        bot.reply_to(message, f"+${amt:.4f} → {tid} = ${nb:.4f}")
        try:
            bot.send_message(tid, f"💳 +${amt:.2f}\nBalance: <b>${nb:.2f}</b>")
        except Exception:
            pass


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").replace("/broadcast", "", 1).strip()
    if not text:
        bot.reply_to(message, "/broadcast សារ...")
        return
    ok = fail = 0
    for s in list(_load().get("users", {}).keys()):
        try:
            bot.send_message(int(s), text)
            ok += 1
        except Exception:
            fail += 1
        time.sleep(0.04)
    bot.reply_to(message, f"Sent {ok}, fail {fail}")


@bot.message_handler(func=lambda m: m.text in ("👤 គណនី",))
def cmd_account(message: types.Message):
    uid = message.from_user.id
    u = get_user(uid)
    db = _load()
    n = sum(1 for o in db.get("orders", {}).values() if o.get("uid") == uid)
    bot.send_message(
        uid,
        f"👤 <b>គណនី</b>\n━━━━━━━━━━━━━━━━\n"
        f"ឈ្មោះ: <b>{u.get('name') or '-'}</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"💳 Balance: <b>${float(u.get('balance', 0)):.2f}</b>\n"
        f"📦 Orders: <b>{n}</b>",
        reply_markup=main_kb(uid),
    )


@bot.message_handler(func=lambda m: m.text in ("💬 Support",))
def cmd_support(message: types.Message):
    bot.send_message(message.chat.id, f"💬 {cfg().get('support_text') or 'Support'}", reply_markup=main_kb(message.from_user.id))


@bot.message_handler(func=lambda m: m.text in ("📋 Services", "🛒 បញ្ជាទិញ"))
def cmd_services(message: types.Message):
    services = get_services()
    if not services:
        bot.send_message(
            message.chat.id,
            "❌ គ្មាន service\nAdmin ត្រូវកំណត់ Panel API Key ក្នុង ⚙️ Setup",
            reply_markup=main_kb(message.from_user.id),
        )
        return
    bot.send_message(
        message.chat.id,
        f"📋 <b>Services</b> ({len(services)})\nជ្រើស Platform:",
        reply_markup=cats_kb(services),
    )


@bot.message_handler(func=lambda m: m.text in ("📦 Orders",))
def cmd_orders(message: types.Message):
    uid = message.from_user.id
    mine = [o for o in _load().get("orders", {}).values() if o.get("uid") == uid]
    mine.sort(key=lambda x: x.get("ts", 0), reverse=True)
    if not mine:
        bot.send_message(uid, "📦 មិនទាន់មាន order", reply_markup=main_kb(uid))
        return
    lines = ["📦 <b>Orders</b>\n━━━━━━━━━━━━━━━━"]
    for o in mine[:15]:
        lines.append(
            f"#{o.get('id')} · {str(o.get('name', '?'))[:28]}\n"
            f"  x{o.get('qty')} · ${float(o.get('charge', 0)):.4f} · <b>{o.get('status')}</b>"
        )
    bot.send_message(uid, "\n".join(lines), reply_markup=main_kb(uid))


@bot.message_handler(func=lambda m: m.text in ("💰 ដាក់លុយ",))
def cmd_deposit(message: types.Message):
    uid = message.from_user.id
    c = cfg()
    u = get_user(uid)
    mn = float(c.get("min_deposit") or MIN_DEPOSIT)
    has_bakong = bool(c.get("bakong_id"))
    has_qr = QR_FILE.exists()
    if not has_bakong and not has_qr:
        bot.send_message(
            uid,
            "⚠️ មិនទាន់មានវិធីទទួលលុយ\nAdmin ត្រូវ Setup Bakong ឬ Upload QR",
            reply_markup=main_kb(uid),
        )
        return
    mode = "Bakong ID: " + c["bakong_id"] if has_bakong else "QR ធម្មតា (រូប)"
    bot.send_message(
        uid,
        f"💰 <b>ដាក់លុយ — {c.get('shop_name')}</b>\n"
        f"Balance: <b>${float(u.get('balance', 0)):.2f}</b>\n"
        f"អប្បបរមា: <b>${mn:.2f}</b>\n"
        f"វិធី: <b>{mode}</b>\n\n"
        f"ជ្រើសចំនួន:",
        reply_markup=deposit_amt_kb(),
    )


# ── Callbacks ───────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: True)
def on_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    data = call.data or ""
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    # Admin config
    if data.startswith("cfg:") and is_admin(uid):
        key = data[4:]
        if key == "upload_qr":
            waiting[uid] = "upload_qr"
            bot.send_message(uid, "📷 ផ្ញើរូប QR (photo) ឥឡូវនេះ\n(ABA / Wing / Acleda / …)", reply_markup=cancel_kb())
            return
        if key == "view_qr":
            if QR_FILE.exists():
                with open(QR_FILE, "rb") as f:
                    bot.send_photo(uid, f, caption="QR បង់លុយបច្ចុប្បន្ន")
            else:
                bot.send_message(uid, "មិនទាន់មាន QR")
            return
        labels = {
            "shop_name": "វាយឈ្មោះ Shop:",
            "bakong_id": "វាយ Bakong Account ID\n(ឧ. name@aclb ឬលេខ — ទុកចោលបើគ្មាន):",
            "bakong_name": "វាយឈ្មោះគណនី Bakong / Merchant:",
            "bakong_city": "វាយទីក្រុង:",
            "currency": "វាយ Currency (USD ឬ KHR):",
            "bakong_token": (
                "វាយ Bakong Developer Token\n"
                "(ចុះឈ្មោះនៅ https://api-bakong.nbc.gov.kh/register/ ចាំបាច់សម្រាប់ auto-verify ការបង់ប្រាក់\n"
                "ទុកចោល «-» បើគ្មាន — QR នៅតែបង្កើតបាន ប៉ុន្តែ Admin ត្រូវ confirm ដោយដៃ):"
            ),
            "panel_url": "វាយ Panel URL (https://...):",
            "panel_api_key": "វាយ Panel API Key:",
            "min_deposit": "វាយ min deposit USD (ឧ. 0.75):",
        }
        if key in labels:
            waiting[uid] = f"cfg:{key}"
            bot.send_message(uid, labels[key], reply_markup=cancel_kb())
        return

    if data == "back_cats":
        services = get_services()
        bot.edit_message_text(
            f"📋 Services ({len(services)})\nជ្រើស Platform:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=cats_kb(services),
        )
        return

    if data.startswith("cat:"):
        cat = data[4:]
        services = get_services()
        bot.edit_message_text(
            f"📂 <b>{cat}</b> — ជ្រើស service:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=services_kb(services, cat),
        )
        return

    if data.startswith("svc:"):
        sid = data[4:]
        services = get_services()
        svc = next((s for s in services if str(s.get("service") or s.get("id")) == str(sid)), None)
        if not svc:
            bot.answer_callback_query(call.id, "Not found", show_alert=True)
            return
        rate = float(svc.get("rate") or 0)
        mn, mx = int(svc.get("min") or 1), int(svc.get("max") or 100000)
        waiting[uid] = f"order:{sid}:{rate}:{mn}:{mx}:{str(svc.get('name', ''))[:50]}"
        bot.send_message(
            uid,
            f"🛒 <b>{svc.get('name')}</b>\n"
            f"${rate:.4f}/1k · Min {mn} · Max {mx}\n\n"
            f"ផ្ញើ <b>quantity</b> (លេខ):",
            reply_markup=cancel_kb(),
        )
        return

    if data.startswith("dep:"):
        part = data[4:]
        if part == "custom":
            waiting[uid] = "dep_custom"
            bot.send_message(uid, f"វាយចំនួន USD (min ${float(cfg().get('min_deposit', MIN_DEPOSIT)):.2f}):", reply_markup=cancel_kb())
            return
        try:
            amount = float(part)
        except ValueError:
            return
        _start_deposit(uid, amount)
        return

    if data.startswith("paid:"):
        # user claims paid
        dep_id = data[5:]
        db = _load()
        dep = db.get("deposits", {}).get(dep_id)
        if not dep or dep.get("uid") != uid:
            return
        if dep.get("status") != "pending":
            bot.send_message(uid, "Deposit នេះមិន pending ទេ")
            return

        # Try live auto-verify against Bakong first (if token configured)
        if check_khqr_paid(dep.get("md5")):
            _confirm_deposit(dep_id, True)
            return

        bot.send_message(uid, "⏳ រង់ចាំ Admin បញ្ជាក់…")
        if ADMIN_ID:
            mk = types.InlineKeyboardMarkup()
            mk.add(
                types.InlineKeyboardButton("✅ Confirm + credit", callback_data=f"adm_ok:{dep_id}"),
                types.InlineKeyboardButton("❌ Reject", callback_data=f"adm_no:{dep_id}"),
            )
            bot.send_message(
                ADMIN_ID,
                f"💸 User អះអាងថាបង់រួច\n"
                f"Dep <code>#{dep_id}</code>\n"
                f"User: <code>{uid}</code>\n"
                f"Amount: <b>${float(dep.get('amount', 0)):.2f}</b>\n"
                f"Shop: {cfg().get('shop_name')}",
                reply_markup=mk,
            )
        return

    if data.startswith("adm_ok:") and is_admin(uid):
        dep_id = data[7:]
        _confirm_deposit(dep_id, True)
        return
    if data.startswith("adm_no:") and is_admin(uid):
        dep_id = data[7:]
        _confirm_deposit(dep_id, False)
        return


def _start_deposit(uid: int, amount: float):
    c = cfg()
    mn = float(c.get("min_deposit") or MIN_DEPOSIT)
    if amount < mn:
        bot.send_message(uid, f"❌ អប្បបរមា ${mn:.2f}", reply_markup=main_kb(uid))
        return
    with _lock:
        db = _load()
        dep_id = db.get("next_dep", 1)
        db["next_dep"] = dep_id + 1
        db.setdefault("deposits", {})[str(dep_id)] = {
            "id": dep_id,
            "uid": uid,
            "amount": amount,
            "status": "pending",
            "ts": time.time(),
        }
        _save(db)

    caption = (
        f"💰 <b>ដាក់លុយ ${amount:.2f}</b>\n"
        f"🏪 {c.get('shop_name')}\n"
        f"Bill: <code>#{dep_id}</code>\n"
    )
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("✅ ខ្ញុំបង់រួច", callback_data=f"paid:{dep_id}"))

    khqr_result = generate_khqr(amount, f"DEP{dep_id}")
    if khqr_result:
        qr_string, md5_hash, png_path = khqr_result
        with _lock:
            db2 = _load()
            db2["deposits"][str(dep_id)]["qr"] = qr_string
            db2["deposits"][str(dep_id)]["md5"] = md5_hash
            _save(db2)
        caption += (
            f"\n🏦 ស្កេន <b>KHQR</b> ខាងក្រោមតាម Bakong / ABA / Wing / Acleda…\n"
            f"ចំនួនទឹកប្រាក់ដាក់រួចរាល់ក្នុង QR — មិនចាំបាច់វាយបញ្ចូល\n"
        )
        if png_path and Path(png_path).exists():
            with open(png_path, "rb") as f:
                bot.send_photo(uid, f, caption=caption, reply_markup=mk)
        else:
            bot.send_message(uid, caption + f"\n<code>{qr_string}</code>", reply_markup=mk)
    elif c.get("bakong_id"):
        caption += (
            f"\n🏦 Bakong ID:\n<code>{c.get('bakong_id')}</code>\n"
            f"👤 {c.get('bakong_name') or c.get('shop_name')}\n"
            f"🏙 {c.get('bakong_city') or ''}\n"
            f"\nផ្ទេរតាម Bakong / KHQR ឲ្យគណនីខាងលើ\n"
        )
        caption += f"\n{c.get('qr_caption') or ''}"
        if QR_FILE.exists():
            with open(QR_FILE, "rb") as f:
                bot.send_photo(uid, f, caption=caption, reply_markup=mk)
        else:
            bot.send_message(uid, caption, reply_markup=mk)
    else:
        caption += f"\n{c.get('qr_caption') or ''}"
        if QR_FILE.exists():
            with open(QR_FILE, "rb") as f:
                bot.send_photo(uid, f, caption=caption, reply_markup=mk)
        else:
            bot.send_message(uid, caption, reply_markup=mk)

    if ADMIN_ID:
        try:
            bot.send_message(
                ADMIN_ID,
                f"🆕 Deposit pending #{dep_id}\nUser <code>{uid}</code>\n${amount:.2f}",
            )
        except Exception:
            pass


def _confirm_deposit(dep_id: str, ok: bool):
    with _lock:
        db = _load()
        dep = db.get("deposits", {}).get(str(dep_id))
        if not dep or dep.get("status") != "pending":
            return
        dep["status"] = "paid" if ok else "rejected"
        db["deposits"][str(dep_id)] = dep
        _save(db)
    uid = int(dep["uid"])
    amount = float(dep["amount"])
    if ok:
        nb = add_balance(uid, amount)
        bot.send_message(uid, f"✅ ដាក់លុយ ${amount:.2f} ជោគជ័យ!\n💳 Balance: <b>${nb:.2f}</b>", reply_markup=main_kb(uid))
        if ADMIN_ID:
            bot.send_message(ADMIN_ID, f"✅ Credited dep #{dep_id} +${amount:.2f} → {uid}")
    else:
        bot.send_message(uid, f"❌ Deposit #{dep_id} ត្រូវបានបដិសេធ", reply_markup=main_kb(uid))


# ── Text / photo ────────────────────────────────────────────────
@bot.message_handler(content_types=["photo"])
def on_photo(message: types.Message):
    uid = message.from_user.id
    if waiting.get(uid) != "upload_qr" or not is_admin(uid):
        return
    waiting.pop(uid, None)
    try:
        file_id = message.photo[-1].file_id
        info = bot.get_file(file_id)
        raw = bot.download_file(info.file_path)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        QR_FILE.write_bytes(raw)
        bot.send_message(
            uid,
            "✅ រក្សាទុក QR រួច\nUser នឹងឃើញរូបនេះពេលដាក់លុយ (សម្រាប់អ្នកគ្មាន Bakong)",
            reply_markup=main_kb(uid),
        )
    except Exception as e:
        bot.send_message(uid, f"❌ Upload failed: {e}", reply_markup=main_kb(uid))


@bot.message_handler(func=lambda m: m.from_user.id in waiting)
def on_waiting(message: types.Message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    state = waiting.get(uid, "")

    if text in ("❌ បោះបង់", "/cancel"):
        waiting.pop(uid, None)
        bot.send_message(uid, "បានបោះបង់។", reply_markup=main_kb(uid))
        return

    if state.startswith("cfg:") and is_admin(uid):
        key = state[4:]
        waiting.pop(uid, None)
        if key == "min_deposit":
            try:
                set_cfg(min_deposit=float(text))
            except ValueError:
                bot.send_message(uid, "លេខមិនត្រឹមត្រូវ", reply_markup=main_kb(uid))
                return
        elif key in ("bakong_id", "bakong_token") and text in ("-", "0", "none", "គ្មាន"):
            set_cfg(**{key: ""})
        elif key == "currency":
            cur = text.strip().upper()
            if cur not in ("USD", "KHR"):
                bot.send_message(uid, "Currency ត្រូវតែ USD ឬ KHR", reply_markup=main_kb(uid))
                return
            set_cfg(currency=cur)
        else:
            set_cfg(**{key: text})
        bot.send_message(uid, f"✅ រក្សាទុក <b>{key}</b>\n<code>{text}</code>", reply_markup=setup_kb())
        return

    if state == "dep_custom":
        try:
            amount = float(text.replace(",", ""))
        except ValueError:
            bot.send_message(uid, "វាយលេខ ឧ. 0.75")
            return
        waiting.pop(uid, None)
        _start_deposit(uid, amount)
        return

    if state.startswith("order:") and state.count(":") >= 5:
        parts = state.split(":", 5)
        _, sid, rate_s, mn_s, mx_s, name = parts
        try:
            qty = int(float(text.replace(",", "")))
        except ValueError:
            bot.send_message(uid, "វាយលេខ quantity")
            return
        mn, mx = int(mn_s), int(mx_s)
        if qty < mn or qty > mx:
            bot.send_message(uid, f"ត្រូវ {mn} – {mx}")
            return
        charge = round((qty / 1000.0) * float(rate_s), 4)
        waiting[uid] = f"link:{sid}:{qty}:{charge}:{name}"
        bot.send_message(
            uid,
            f"Qty: <b>{qty:,}</b> · Charge: <b>${charge:.4f}</b>\nផ្ញើ <b>Link</b>:",
            reply_markup=cancel_kb(),
        )
        return

    if state.startswith("link:"):
        parts = state.split(":", 4)
        if len(parts) < 5:
            waiting.pop(uid, None)
            return
        _, sid, qty_s, charge_s, name = parts
        link = text
        if len(link) < 3:
            bot.send_message(uid, "Link ខ្លីពេក")
            return
        waiting.pop(uid, None)
        qty, charge = int(qty_s), float(charge_s)
        ok, bal = deduct_balance(uid, charge)
        if not ok:
            bot.send_message(
                uid,
                f"❌ លុយមិនគ្រប់ (ត្រូវ ${charge:.4f} · មាន ${bal:.4f})",
                reply_markup=main_kb(uid),
            )
            return
        bot.send_message(uid, "⏳ បញ្ជូនទៅ panel…")
        res = panel("add", service=str(sid), quantity=str(qty), link=link)
        if isinstance(res, dict) and res.get("error"):
            add_balance(uid, charge)
            bot.send_message(uid, f"❌ {res['error']}\nសងលុយវិញហើយ", reply_markup=main_kb(uid))
            return
        panel_oid = res.get("order") if isinstance(res, dict) else None
        with _lock:
            db = _load()
            oid = db.get("next_order", 1)
            db["next_order"] = oid + 1
            db.setdefault("orders", {})[str(oid)] = {
                "id": oid,
                "uid": uid,
                "service_id": sid,
                "name": name,
                "qty": qty,
                "charge": charge,
                "link": link,
                "panel_order": panel_oid,
                "status": "processing",
                "ts": time.time(),
            }
            _save(db)
        nb = float(get_user(uid).get("balance", 0))
        bot.send_message(
            uid,
            f"✅ Order <code>#{oid}</code>\n{name}\nx{qty:,} · ${charge:.4f}\n"
            f"Panel: <code>{panel_oid or '-'}</code>\n💳 ${nb:.2f}",
            reply_markup=main_kb(uid),
        )
        if ADMIN_ID:
            try:
                bot.send_message(ADMIN_ID, f"🛒 #{oid} user {uid}\n{name} x{qty} ${charge:.4f}")
            except Exception:
                pass
        return

    waiting.pop(uid, None)
    bot.send_message(uid, "ជ្រើសពីម៉ឺនុយ", reply_markup=main_kb(uid))


@bot.message_handler(func=lambda m: True)
def fallback(message: types.Message):
    bot.send_message(message.chat.id, "ជ្រើសពីម៉ឺនុយ 👇", reply_markup=main_kb(message.from_user.id))


def main():
    log.info("Shop bot starting | ADMIN_ID=%s", ADMIN_ID)
    log.info("Data: %s", DATA_DIR)
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=15)
        except Exception as e:
            log.warning("Polling error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
