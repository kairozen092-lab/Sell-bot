#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kairozen — លក់ Bot SMM ($0.75)
មិនផ្ញើ file។ ពេលអ្នកទិញបង់លុយ + បំពេញ Bot Token → bot SMM រត់ភ្លាម (subprocess).

Env:
  BOT_TOKEN, ADMIN_ID, PRICE_USD=0.75
  SHOP_BOT_SCRIPT=kairozen_shop_bot.py
  PANEL_URL, INSTANCES_DIR

Bakong KHQR (បង្កើត QR ស្វ័យប្រវត្តិសម្រាប់ទទួលការទិញ $0.75):
  pip install "bakong-khqr[image]" qrcode
  BAKONG_ID       (ចាំបាច់) — Bakong account/Account ID ដែលទទួលលុយ (ឧ. name@aclb)
  BAKONG_NAME     ឈ្មោះគណនី (default: Kairozen)
  BAKONG_CITY     ទីក្រុង (default: Phnom Penh)
  BAKONG_CURRENCY USD ឬ KHR (default: USD)
  BAKONG_TOKEN    (ស្រេចចិត្ត) Bakong Developer Token — https://api-bakong.nbc.gov.kh/register/
                  → បើមាន Token: auto-verify + auto-credit ភ្លាមៗ
                  → បើគ្មាន: QR នៅតែបង្កើតបាន ប៉ុន្តែត្រូវ Admin ចុច Confirm ដោយដៃ
"""
from __future__ import annotations

import json, logging, os, signal, subprocess, sys, time
from pathlib import Path
import telebot
from telebot import types

try:
    from bakong_khqr import KHQR  # pip install "bakong-khqr[image]" qrcode
except Exception:
    KHQR = None

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("sell-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PRICE = float(os.getenv("PRICE_USD", "0.75"))
BASE = Path(__file__).resolve().parent
SHOP_SCRIPT = Path(os.getenv("SHOP_BOT_SCRIPT") or (BASE / "kairozen_shop_bot.py"))
PANEL_URL = (os.getenv("PANEL_URL") or "https://kairozen-smm.onrender.com").rstrip("/")
INSTANCES_DIR = Path(os.getenv("INSTANCES_DIR") or (BASE / "instances"))
DB_FILE = Path(os.getenv("DB_FILE") or (BASE / "sell_bot_db.json"))
PRODUCT_NAME = "Kairozen SMM Bot"

# Bakong (your own account, receives the $0.75 sale) — optional but recommended.
BAKONG_ID = os.getenv("BAKONG_ID", "").strip()
BAKONG_NAME = os.getenv("BAKONG_NAME", "Kairozen").strip()
BAKONG_CITY = os.getenv("BAKONG_CITY", "Phnom Penh").strip()
BAKONG_CURRENCY = os.getenv("BAKONG_CURRENCY", "USD").strip().upper()
BAKONG_TOKEN = os.getenv("BAKONG_TOKEN", "").strip()  # https://api-bakong.nbc.gov.kh/register/
QR_DIR = BASE / "khqr_cache"

if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN")


def generate_khqr(amount: float, bill_number: str):
    """Live Bakong KHQR (amount baked in) + QR png. Returns (qr, md5, png_path) or None."""
    if not KHQR or not BAKONG_ID:
        return None
    try:
        khqr = KHQR(BAKONG_TOKEN or "no-token")
        qr_string = khqr.create_qr(
            account_id=BAKONG_ID,
            merchant_name=BAKONG_NAME[:25],
            merchant_city=BAKONG_CITY[:15],
            amount=round(float(amount), 2),
            currency=BAKONG_CURRENCY,
            bill_number=str(bill_number)[:25],
            store_label=PRODUCT_NAME[:25],
            static=False,
            expiration=1,
        )
        md5_hash = khqr.generate_md5(qr_string)
    except Exception as e:
        log.warning("KHQR create_qr failed: %s", e)
        return None
    QR_DIR.mkdir(parents=True, exist_ok=True)
    png_path = str(QR_DIR / f"{bill_number}.png")
    try:
        import qrcode
        qrcode.make(qr_string).save(png_path)
    except Exception as e:
        log.warning("QR image render failed: %s", e)
        png_path = None
    return qr_string, md5_hash, png_path


def check_khqr_paid(md5_hash: str) -> bool:
    if not KHQR or not md5_hash or not BAKONG_TOKEN:
        return False
    try:
        khqr = KHQR(BAKONG_TOKEN)
        return khqr.check_payment(md5_hash) == "PAID"
    except Exception as e:
        log.warning("check_payment failed: %s", e)
        return False

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
waiting: dict[int, str] = {}
_processes: dict[str, subprocess.Popen] = {}


def load_db() -> dict:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"buyers": {}, "orders": {}, "instances": {}, "next_id": 1}


def save_db(db: dict) -> None:
    DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def main_kb(uid: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("🛒 ទិញ Bot SMM"))
    kb.add(types.KeyboardButton("🤖 Bot របស់ខ្ញុំ"), types.KeyboardButton("ℹ️ ព័ត៌មាន"))
    kb.add(types.KeyboardButton("💬 Support"))
    if uid == ADMIN_ID:
        kb.add(types.KeyboardButton("⚙️ Admin"))
    return kb


def buy_kb() -> types.InlineKeyboardMarkup:
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton(f"✅ បង់ ${PRICE:.2f} — ទិញឥឡូវ", callback_data="buy_now"))
    return mk


def _instance_dir(uid: int) -> Path:
    d = INSTANCES_DIR / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def stop_customer_bot(uid: int) -> None:
    key = str(uid)
    proc = _processes.pop(key, None)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    db = load_db()
    if key in db.get("instances", {}):
        db["instances"][key]["status"] = "stopped"
        db["instances"][key]["stopped"] = time.time()
        save_db(db)


def start_customer_bot(uid: int, customer_token: str) -> tuple[bool, str]:
    if not SHOP_SCRIPT.exists():
        return False, f"Script not found: {SHOP_SCRIPT}"
    stop_customer_bot(uid)
    data_dir = _instance_dir(uid)
    log_path = data_dir / "bot.log"
    env = os.environ.copy()
    env["BOT_TOKEN"] = customer_token.strip()
    env["ADMIN_ID"] = str(uid)
    env["PANEL_URL"] = PANEL_URL
    env["BOT_DATA_DIR"] = str(data_dir)
    env["MIN_DEPOSIT"] = "0.75"
    env.pop("PANEL_API_KEY", None)
    log_f = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(SHOP_SCRIPT)],
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=str(BASE),
            start_new_session=True,
        )
    except Exception as e:
        log_f.close()
        return False, str(e)
    _processes[str(uid)] = proc
    db = load_db()
    db.setdefault("instances", {})[str(uid)] = {
        "uid": uid,
        "pid": proc.pid,
        "token": customer_token.strip(),
        "token_tail": customer_token.strip()[-6:],
        "started": time.time(),
        "data_dir": str(data_dir),
        "status": "running",
    }
    save_db(db)
    log.info("Started bot uid=%s pid=%s", uid, proc.pid)
    return True, f"pid={proc.pid}"


def is_running(uid: int) -> bool:
    proc = _processes.get(str(uid))
    if proc and proc.poll() is None:
        return True
    inst = load_db().get("instances", {}).get(str(uid))
    if not inst or not inst.get("pid"):
        return False
    try:
        os.kill(int(inst["pid"]), 0)
        return True
    except Exception:
        return False


@bot.message_handler(commands=["start", "help"])
def cmd_start(m: types.Message):
    uid = m.from_user.id
    bot.send_message(
        uid,
        f"សួស្តី <b>{m.from_user.first_name or 'User'}</b>!\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🤖 លក់ <b>{PRODUCT_NAME}</b>\n"
        f"💵 តម្លៃ: <b>${PRICE:.2f}</b>\n\n"
        f"ទិញរួច:\n"
        f"1. បង្កើត bot នៅ @BotFather → Token\n"
        f"2. បិទ Token មកទីនេះ\n"
        f"3. Bot របស់អ្នក <b>រត់ស្វ័យប្រវត្តិ</b>\n"
        f"   (មិនផ្ញើ file)\n"
        f"4. ចូល bot ថ្មី → ⚙️ Setup\n\n"
        f"ចុច <b>🛒 ទិញ Bot SMM</b>",
        reply_markup=main_kb(uid),
    )


@bot.message_handler(func=lambda m: m.text in ("ℹ️ ព័ត៌មាន",))
def cmd_info(m: types.Message):
    bot.send_message(
        m.chat.id,
        f"ℹ️ <b>{PRODUCT_NAME}</b> · ${PRICE:.2f}\n\n"
        f"• មិន download file\n"
        f"• បំពេញ Bot Token → bot រត់ភ្លាម\n"
        f"• Setup: Shop, Bakong ឬ QR ធម្មតា\n"
        f"• ភ្ជាប់ website API ក្នុង Setup",
        reply_markup=main_kb(m.from_user.id),
    )


@bot.message_handler(func=lambda m: m.text == "🛒 ទិញ Bot SMM")
def cmd_buy(m: types.Message):
    bot.send_message(
        m.chat.id,
        f"🛒 <b>ទិញ {PRODUCT_NAME}</b>\n💵 <b>${PRICE:.2f}</b>\n\n"
        f"បង់រួច → បំពេញ Bot Token → bot រត់ភ្លាម",
        reply_markup=buy_kb(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "buy_now")
def on_buy(call: types.CallbackQuery):
    uid = call.from_user.id
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    db = load_db()
    if db.get("buyers", {}).get(str(uid), {}).get("paid"):
        waiting[uid] = "token"
        bot.send_message(uid, "✅ ទិញរួចហើយ។\nផ្ញើ <b>Bot Token</b> ពី @BotFather:", reply_markup=types.ReplyKeyboardRemove())
        return
    oid = db.get("next_id", 1)
    db["next_id"] = oid + 1
    order = {
        "id": oid, "uid": uid, "name": call.from_user.first_name or "",
        "username": call.from_user.username or "", "amount": PRICE,
        "status": "pending", "ts": time.time(),
    }
    db.setdefault("orders", {})[str(oid)] = order
    save_db(db)

    khqr_result = generate_khqr(PRICE, f"SMM{oid}")
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("✅ ខ្ញុំបង់រួច", callback_data=f"paid:{oid}"))
    if ADMIN_ID:
        mk.add(types.InlineKeyboardButton("✅ Admin Confirm", callback_data=f"admin_ok:{oid}"))

    if khqr_result:
        qr_string, md5_hash, png_path = khqr_result
        with_db = load_db()
        with_db["orders"][str(oid)]["md5"] = md5_hash
        save_db(with_db)
        caption = (
            f"🧾 Order <code>#{oid}</code>\n{PRODUCT_NAME} · ${PRICE:.2f}\n\n"
            f"🏦 ស្កេន <b>KHQR</b> តាម Bakong / ABA / Wing / Acleda…\n"
            f"ចំនួនទឹកប្រាក់ដាក់រួចរាល់ក្នុង QR"
        )
        if png_path and Path(png_path).exists():
            with open(png_path, "rb") as f:
                bot.send_photo(uid, f, caption=caption, reply_markup=mk)
        else:
            bot.send_message(uid, caption + f"\n<code>{qr_string}</code>", reply_markup=mk)
    else:
        mk.add(types.InlineKeyboardButton(f"💳 បញ្ជាក់បង់ ${PRICE:.2f} (Demo)", callback_data=f"pay:{oid}"))
        bot.send_message(uid, f"🧾 Order <code>#{oid}</code>\n{PRODUCT_NAME} · ${PRICE:.2f}", reply_markup=mk)

    if ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID, f"🛒 #{oid} user <code>{uid}</code> ${PRICE:.2f}")
        except Exception:
            pass


def _mark_paid(db: dict, oid: str, uid: int) -> None:
    db["orders"][oid]["status"] = "paid"
    db["orders"][oid]["paid_at"] = time.time()
    db.setdefault("buyers", {})[str(uid)] = {"paid": True, "order_id": int(oid), "ts": time.time()}
    save_db(db)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("paid:"))
def on_paid_claim(call: types.CallbackQuery):
    """User clicked 'ខ្ញុំបង់រួច' after scanning a real KHQR."""
    uid = call.from_user.id
    oid = call.data.split(":")[1]
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    db = load_db()
    order = db.get("orders", {}).get(oid)
    if not order or order.get("uid") != uid:
        bot.send_message(uid, "Invalid order")
        return
    if order.get("status") == "paid":
        waiting[uid] = "token"
        bot.send_message(uid, "✅ បង់រួចហើយ។\nផ្ញើ <b>Bot Token</b> ពី @BotFather:", reply_markup=types.ReplyKeyboardRemove())
        return
    if check_khqr_paid(order.get("md5")):
        _mark_paid(db, oid, uid)
        waiting[uid] = "token"
        bot.send_message(
            uid,
            f"✅ បង់ ${PRICE:.2f} ជោគជ័យ!\n\n"
            f"បង្កើត bot នៅ @BotFather → /newbot → យក Token\n"
            f"ផ្ញើ Token មកទីនេះ:\n<code>123456789:AAF...</code>",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        return
    bot.send_message(uid, "⏳ រង់ចាំ Admin បញ្ជាក់ការបង់ប្រាក់…")
    if ADMIN_ID:
        try:
            mk = types.InlineKeyboardMarkup()
            mk.add(types.InlineKeyboardButton("✅ Admin Confirm", callback_data=f"admin_ok:{oid}"))
            bot.send_message(
                ADMIN_ID,
                f"💸 User អះអាងថាបង់រួច\nOrder <code>#{oid}</code>\nUser: <code>{uid}</code>\n${PRICE:.2f}",
                reply_markup=mk,
            )
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pay:"))
def on_pay(call: types.CallbackQuery):
    uid = call.from_user.id
    oid = call.data.split(":")[1]
    db = load_db()
    order = db.get("orders", {}).get(oid)
    if not order or order.get("uid") != uid:
        bot.answer_callback_query(call.id, "Invalid", show_alert=True)
        return
    if order.get("status") != "paid":
        _mark_paid(db, oid, uid)
    try:
        bot.answer_callback_query(call.id, "OK")
    except Exception:
        pass
    waiting[uid] = "token"
    bot.send_message(
        uid,
        f"✅ បង់ ${PRICE:.2f} ជោគជ័យ!\n\n"
        f"បង្កើត bot នៅ @BotFather → /newbot → យក Token\n"
        f"ផ្ញើ Token មកទីនេះ:\n<code>123456789:AAF...</code>",
        reply_markup=types.ReplyKeyboardRemove(),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("admin_ok:"))
def on_admin_ok(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Admin only", show_alert=True)
        return
    oid = call.data.split(":")[1]
    db = load_db()
    order = db.get("orders", {}).get(oid)
    if not order:
        bot.answer_callback_query(call.id, "Not found", show_alert=True)
        return
    uid = int(order["uid"])
    if order.get("status") != "paid":
        _mark_paid(db, oid, uid)
    bot.answer_callback_query(call.id, "Confirmed")
    waiting[uid] = "token"
    bot.send_message(call.message.chat.id, f"✅ Order #{oid} paid")
    try:
        bot.send_message(uid, f"✅ បង់លុយរួច — ផ្ញើ <b>Bot Token</b>:", reply_markup=types.ReplyKeyboardRemove())
    except Exception:
        pass


@bot.message_handler(func=lambda m: waiting.get(m.from_user.id) == "token")
def on_token(m: types.Message):
    uid = m.from_user.id
    token = (m.text or "").strip()
    if ":" not in token or len(token) < 30:
        bot.send_message(uid, "Token មិនត្រឹមត្រូវ។ ទម្រង់: <code>123456:AA....</code>")
        return
    db = load_db()
    if not db.get("buyers", {}).get(str(uid), {}).get("paid"):
        waiting.pop(uid, None)
        bot.send_message(uid, "មិនទាន់បង់លុយ", reply_markup=main_kb(uid))
        return
    waiting.pop(uid, None)
    bot.send_message(uid, "⏳ កំពុងចាប់ផ្ដើម bot របស់អ្នក…")
    ok, info = start_customer_bot(uid, token)
    if not ok:
        bot.send_message(uid, f"❌ មិនអាចរត់ bot:\n<code>{info}</code>", reply_markup=main_kb(uid))
        if ADMIN_ID:
            bot.send_message(ADMIN_ID, f"Start fail {uid}: {info}")
        return
    bot.send_message(
        uid,
        f"✅ <b>Bot របស់អ្នកកំពុងដំណើរការ!</b>\n"
        f"{info}\n\n"
        f"1. បើក bot ថ្មីក្នុង Telegram → /start\n"
        f"2. ចុច <b>⚙️ Setup</b>:\n"
        f"   • ឈ្មោះ Shop\n"
        f"   • Bakong ID + ឈ្មោះ (បើមាន)\n"
        f"   • ឬ Upload QR ធម្មតា\n"
        f"   • Panel API Key\n\n"
        f"មិនផ្ញើ file — bot រត់លើ server ហើយ។",
        reply_markup=main_kb(uid),
    )
    if ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID, f"🚀 Instance uid=<code>{uid}</code> {info}")
        except Exception:
            pass


@bot.message_handler(func=lambda m: m.text == "🤖 Bot របស់ខ្ញុំ")
def cmd_my_bot(m: types.Message):
    uid = m.from_user.id
    db = load_db()
    if not db.get("buyers", {}).get(str(uid), {}).get("paid"):
        bot.send_message(uid, "មិនទាន់ទិញ", reply_markup=main_kb(uid))
        return
    inst = db.get("instances", {}).get(str(uid)) or {}
    running = is_running(uid)
    mk = types.InlineKeyboardMarkup()
    if running:
        mk.add(types.InlineKeyboardButton("⏹ Stop", callback_data="inst_stop"))
    else:
        mk.add(types.InlineKeyboardButton("▶️ Start", callback_data="inst_start"))
    mk.add(types.InlineKeyboardButton("🔑 ប្តូរ Token", callback_data="inst_token"))
    bot.send_message(
        uid,
        f"🤖 Bot របស់អ្នក\n"
        f"Status: {'🟢 រត់' if running else '🔴 ឈប់'}\n"
        f"PID: <code>{inst.get('pid', '-')}</code>\n"
        f"Token: …{inst.get('token_tail', '—')}",
        reply_markup=mk,
    )


@bot.callback_query_handler(func=lambda c: c.data in ("inst_stop", "inst_start", "inst_token"))
def on_inst(call: types.CallbackQuery):
    uid = call.from_user.id
    db = load_db()
    if not db.get("buyers", {}).get(str(uid), {}).get("paid"):
        bot.answer_callback_query(call.id, "Not paid", show_alert=True)
        return
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if call.data == "inst_stop":
        stop_customer_bot(uid)
        bot.send_message(uid, "⏹ បានបញ្ឈប់", reply_markup=main_kb(uid))
        return
    if call.data == "inst_token":
        waiting[uid] = "token"
        bot.send_message(uid, "ផ្ញើ Bot Token ថ្មី:")
        return
    token = (db.get("instances", {}).get(str(uid)) or {}).get("token")
    if not token:
        waiting[uid] = "token"
        bot.send_message(uid, "ផ្ញើ Bot Token:")
        return
    ok, info = start_customer_bot(uid, token)
    bot.send_message(uid, f"{'✅' if ok else '❌'} {info}", reply_markup=main_kb(uid))


@bot.message_handler(func=lambda m: m.text == "💬 Support")
def cmd_support(m: types.Message):
    bot.send_message(m.chat.id, "💬 Support\n" + (f"Admin: <code>{ADMIN_ID}</code>" if ADMIN_ID else ""), reply_markup=main_kb(m.from_user.id))


@bot.message_handler(func=lambda m: m.text == "⚙️ Admin" and m.from_user.id == ADMIN_ID)
def cmd_admin(m: types.Message):
    db = load_db()
    running = sum(1 for u in db.get("instances", {}) if is_running(int(u)))
    bot.send_message(
        m.chat.id,
        f"⚙️ Admin\nOrders: {len(db.get('orders', {}))}\nBuyers: {len(db.get('buyers', {}))}\n"
        f"Running: {running}\nScript: <code>{SHOP_SCRIPT}</code>",
        reply_markup=main_kb(m.from_user.id),
    )


@bot.message_handler(func=lambda m: True)
def fallback(m: types.Message):
    if waiting.get(m.from_user.id):
        return
    bot.send_message(m.chat.id, "ជ្រើសពីម៉ឺនុយ 👇", reply_markup=main_kb(m.from_user.id))


def restore_instances():
    db = load_db()
    for uid_s, inst in list(db.get("instances", {}).items()):
        token = inst.get("token")
        if not token or inst.get("status") == "stopped":
            continue
        ok, info = start_customer_bot(int(uid_s), token)
        log.info("Restore %s %s %s", uid_s, ok, info)


if __name__ == "__main__":
    log.info("Sell bot $%.2f script=%s", PRICE, SHOP_SCRIPT)
    INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    restore_instances()
    bot.infinity_polling(timeout=20, long_polling_timeout=15)
