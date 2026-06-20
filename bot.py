import asyncio
import re
import sqlite3
import requests
from datetime import datetime, date, timedelta
from contextlib import closing

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiocryptopay import AioCryptoPay, Networks


# ───────────────────────── НАСТРОЙКИ ──────────────────────────────────────
TOKEN          = "8835994765:AAGio2gDLRLknD4-KjeBpSEuN2fnRfrXZ0I"
CRYPTO_BOT_API = "587097:AAdldQwK5tZdd01WdtylzeRSfnIaZzwCKpG"
SUPPORT        = "@VeloxPay_support1"
ADMIN_ID       = 1891161256
MIN_RUB        = 40
RATES_TTL      = 30

# ─── Реферальная программа ───
REF_PERCENT      = 10     # % от суммы обмена (в USDT-эквиваленте), начисляемый рефереру
MAX_REF_REWARD   = 50.0   # максимум начисления за одну заявку, USDT
MIN_REF_WITHDRAW = 5.0    # минимальная сумма вывода реферального баланса, USDT

CRYPTO_ADDRESSES = {
    "USDT": "TXN1SLgXdPCeVonpFneXNYrVM1C2yVD3eE",
    "BTC" : "1GZJDHk8HRfqKKdsjE8ZtMKkPCgPkbvv2K",
    "TON" : "UQD8TRB9wPZyAe_sX1vZ8kGvGxrtKZdpzesvyNFLQr_JNfv8",
    "ETH" : "0xf7f67e98e20c5a4f464aa4668d80e569c75536aa",
    "SOL" : "71G6jvB5bBfAcbMzWzob5uGH1sHSvYdQoVwHTX9k3PJu",
}

COUNTRY_META = {
    "🇷🇺 Россия"   : ("🇷🇺", "RUB", "Рублей", "rub"),
    "🇰🇿 Казахстан": ("🇰🇿", "KZT", "Тенге",  "kzt"),
    "🇺🇦 Украина"  : ("🇺🇦", "UAH", "Гривен", "uah"),
    "🇵🇱 Польша"   : ("🇵🇱", "PLN", "Злотых", "pln"),
    "🇧🇾 Беларусь" : ("🇧🇾", "BYR", "Рублей", "byn"),
    "🇲🇩 Молдова"  : ("🇲🇩", "MDL", "Леев",   "mdl"),
    "🇷🇴 Румыния"  : ("🇷🇴", "RON", "Леев",   "ron"),
}

COINGECKO_IDS = {
    "USDT": "tether",
    "BTC" : "bitcoin",
    "TON" : "the-open-network",
    "ETH" : "ethereum",
    "SOL" : "solana",
}

FALLBACK_RUB  = {"USDT": 90, "BTC": 8_500_000, "TON": 270, "ETH": 320_000, "SOL": 14_000}
FALLBACK_COEF = {"rub": 1.0, "kzt": 5.2, "uah": 3.8, "pln": 0.33, "byn": 0.30, "mdl": 1.85, "ron": 0.50, "usd": 0.011}

# ───────────────────────── СОСТОЯНИЯ FSM ──────────────────────────────────
class ExchangeState(StatesGroup):
    waiting_for_amount = State()

class BroadcastState(StatesGroup):
    waiting_for_text = State()

class CardState(StatesGroup):
    waiting_for_card = State()

class BlockState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_duration = State()

class UnblockState(StatesGroup):
    waiting_for_user_id = State()

class ModBlockState(StatesGroup):
    waiting_for_user_id  = State()
    waiting_for_duration = State()

class AdminMessageState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_text    = State()

class SetRateState(StatesGroup):
    waiting_for_crypto   = State()
    waiting_for_value    = State()

class RefWithdrawState(StatesGroup):
    waiting_card    = State()
    waiting_address = State()

# ───────────────────────── ДАННЫЕ ─────────────────────────────────────────
stats = {
    "unique_today"  : set(),
    "today_date"    : date.today(),
    "all_users"     : set(),
    "all_users_info": {},
    "withdrawals"   : [],
}
blocked_users       = {}
moderators          = set()   # ID модераторов, назначенных создателем (через /addmod)
_withdrawal_counter = 0
_rates_cache        = {}
_manual_rates       = {}   # ручные коэффициенты комиссии: {символ: float}
user_data           = {}

# ═══════════════════════════════════════════════════════════════════════════
#  РЕФЕРАЛЬНАЯ СИСТЕМА — хранилище SQLite (таблицы создаются в init_ref_db)
#  referrals / ref_rewards / ref_withdraw_requests / ref_withdraw_history
# ═══════════════════════════════════════════════════════════════════════════
REF_DB_PATH = "referrals.db"

def _ref_connect():
    conn = sqlite3.connect(REF_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _ref_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def init_ref_db():
    with closing(_ref_connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                user_id     INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                username    TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ref_rewards (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id          INTEGER NOT NULL,
                referred_id          INTEGER NOT NULL,
                withdrawal_id        INTEGER,
                exchange_amount_usdt REAL NOT NULL,
                reward_amount        REAL NOT NULL,
                created_at           TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ref_withdraw_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                amount       REAL NOT NULL,
                method       TEXT NOT NULL,        -- 'card' | 'crypto'
                network      TEXT,                  -- TRC20/TON/ERC20/SOL (если crypto)
                requisites   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
                created_at   TEXT NOT NULL,
                processed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ref_withdraw_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER,
                user_id    INTEGER NOT NULL,
                amount     REAL NOT NULL,
                method     TEXT NOT NULL,
                network    TEXT,
                requisites TEXT NOT NULL,
                status     TEXT NOT NULL,           -- approved/rejected
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()

# ── Реферер ──
def get_referrer(user_id):
    with closing(_ref_connect()) as conn:
        row = conn.execute(
            "SELECT referrer_id FROM referrals WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["referrer_id"] if row else None

def set_referrer(user_id, referrer_id, username=None):
    """INSERT OR IGNORE: реферер сохраняется только при первом запуске
    и не может быть изменён позже. Самореферал блокируется отдельно."""
    if user_id == referrer_id:
        return False
    with closing(_ref_connect()) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO referrals (user_id, referrer_id, username, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, referrer_id, username or "", _ref_now()),
        )
        conn.commit()
        return cur.rowcount > 0

def get_referred_list(referrer_id):
    with closing(_ref_connect()) as conn:
        rows = conn.execute(
            "SELECT user_id, username, created_at FROM referrals "
            "WHERE referrer_id = ? ORDER BY created_at DESC",
            (referrer_id,),
        ).fetchall()
        return [dict(r) for r in rows]

def get_invited_count(referrer_id):
    with closing(_ref_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (referrer_id,)
        ).fetchone()
        return row["c"] if row else 0

def get_active_count(referrer_id):
    """Активный = реферал с хотя бы одним начислением (завершённый обмен)."""
    with closing(_ref_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT referred_id) AS c FROM ref_rewards WHERE referrer_id = ?",
            (referrer_id,),
        ).fetchone()
        return row["c"] if row else 0

# ── Начисления ──
def add_reward(referrer_id, referred_id, withdrawal_id, exchange_amount_usdt, reward_amount):
    with closing(_ref_connect()) as conn:
        conn.execute(
            "INSERT INTO ref_rewards "
            "(referrer_id, referred_id, withdrawal_id, exchange_amount_usdt, reward_amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (referrer_id, referred_id, withdrawal_id, exchange_amount_usdt, reward_amount, _ref_now()),
        )
        conn.commit()

def get_rewards_history(referrer_id, limit=10):
    with closing(_ref_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM ref_rewards WHERE referrer_id = ? ORDER BY id DESC LIMIT ?",
            (referrer_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

def get_total_earned(referrer_id):
    with closing(_ref_connect()) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(reward_amount), 0) AS s FROM ref_rewards WHERE referrer_id = ?",
            (referrer_id,),
        ).fetchone()
        return round(row["s"] or 0, 2)

# ── Баланс / вывод ──
def get_total_withdrawn(user_id):
    with closing(_ref_connect()) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM ref_withdraw_history "
            "WHERE user_id = ? AND status = 'approved'",
            (user_id,),
        ).fetchone()
        return round(row["s"] or 0, 2)

def get_pending_amount(user_id):
    with closing(_ref_connect()) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM ref_withdraw_requests "
            "WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()
        return round(row["s"] or 0, 2)

def get_balance(user_id):
    earned    = get_total_earned(user_id)
    withdrawn = get_total_withdrawn(user_id)
    pending   = get_pending_amount(user_id)
    available = round(earned - withdrawn - pending, 2)
    if available < 0:
        available = 0.0
    return {"earned": earned, "withdrawn": withdrawn, "pending": pending, "available": available}

def has_pending_request(user_id):
    """Защита от двойного вывода: пока есть необработанная заявка — новую создать нельзя."""
    with closing(_ref_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_withdraw_requests WHERE user_id = ? AND status = 'pending' LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None

def create_withdraw_request(user_id, amount, method, network, requisites):
    with closing(_ref_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO ref_withdraw_requests "
            "(user_id, amount, method, network, requisites, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, amount, method, network, requisites, _ref_now()),
        )
        conn.commit()
        return cur.lastrowid

def get_withdraw_request(request_id):
    with closing(_ref_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM ref_withdraw_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

def set_withdraw_request_status(request_id, status):
    with closing(_ref_connect()) as conn:
        conn.execute(
            "UPDATE ref_withdraw_requests SET status = ?, processed_at = ? WHERE id = ?",
            (status, _ref_now(), request_id),
        )
        conn.commit()

def add_withdraw_history(request_id, user_id, amount, method, network, requisites, status):
    with closing(_ref_connect()) as conn:
        conn.execute(
            "INSERT INTO ref_withdraw_history "
            "(request_id, user_id, amount, method, network, requisites, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, user_id, amount, method, network, requisites, status, _ref_now()),
        )
        conn.commit()

def get_withdraw_history(user_id, limit=10):
    with closing(_ref_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM ref_withdraw_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

# ───────────────────────── AIOGRAM ────────────────────────────────────────
storage    = MemoryStorage()
bot        = Bot(token=TOKEN)
dp         = Dispatcher(bot, storage=storage)
crypto_pay = AioCryptoPay(token=CRYPTO_BOT_API, network=Networks.MAIN_NET)

# ───────────────────────── КЛАВИАТУРЫ ─────────────────────────────────────
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("💸 Обменять крипту"))
main_menu.add(KeyboardButton("📜 История операций"), KeyboardButton("❓ FAQ"))
main_menu.add(KeyboardButton("👥 Реферальная программа"))
main_menu.add(KeyboardButton("🛠 Поддержка 24/7"))

admin_menu = ReplyKeyboardMarkup(resize_keyboard=True)
admin_menu.add(KeyboardButton("📊 Статистика"))
admin_menu.add(KeyboardButton("👥 Пользователи"), KeyboardButton("📋 Заявки"))
admin_menu.add(KeyboardButton("📣 Рассылка"),     KeyboardButton("✉️ Написать юзеру"))
admin_menu.add(KeyboardButton("💹 Курс/комиссия"),KeyboardButton("📈 Топ крипты"))
admin_menu.add(KeyboardButton("🚫 Заблокировать"),KeyboardButton("✅ Разблокировать"))
admin_menu.add(KeyboardButton("🔙 Выйти из панели"))

mod_menu = ReplyKeyboardMarkup(resize_keyboard=True)
mod_menu.add(KeyboardButton("📋 Заявки"))
mod_menu.add(KeyboardButton("🚫 Заблокировать"))
mod_menu.add(KeyboardButton("🔙 Выйти из панели"))

countries_menu = ReplyKeyboardMarkup(resize_keyboard=True)
for _c in COUNTRY_META.keys():
    countries_menu.add(_c)
countries_menu.add("⬅️ Назад")

crypto_menu = ReplyKeyboardMarkup(resize_keyboard=True)
crypto_menu.add("USDT", "BTC")
crypto_menu.add("TON",  "ETH")
crypto_menu.add("SOL")
crypto_menu.add("⬅️ Назад")

block_duration_kb = InlineKeyboardMarkup(row_width=2)
block_duration_kb.add(
    InlineKeyboardButton("1 час",    callback_data="ban_1h"),
    InlineKeyboardButton("24 часа",  callback_data="ban_24h"),
    InlineKeyboardButton("7 дней",   callback_data="ban_7d"),
    InlineKeyboardButton("30 дней",  callback_data="ban_30d"),
    InlineKeyboardButton("Навсегда", callback_data="ban_forever"),
)

# ── Реферальная программа: клавиатуры ──
ref_menu = ReplyKeyboardMarkup(resize_keyboard=True)
ref_menu.add(KeyboardButton("🔗 Моя ссылка"), KeyboardButton("👥 Приглашённые"))
ref_menu.add(KeyboardButton("📊 Статистика"), KeyboardButton("💰 Баланс"))
ref_menu.add(KeyboardButton("📜 История"), KeyboardButton("💳 Вывести бонус"))
ref_menu.add(KeyboardButton("⬅️ Назад"))

ref_withdraw_method_kb = InlineKeyboardMarkup(row_width=1)
ref_withdraw_method_kb.add(
    InlineKeyboardButton("💳 Банковская карта", callback_data="refw_method:card"),
    InlineKeyboardButton("🪙 Криптокошелёк",     callback_data="refw_method:crypto"),
)

ref_network_kb = InlineKeyboardMarkup(row_width=2)
ref_network_kb.add(
    InlineKeyboardButton("TRC20", callback_data="refw_net:TRC20"),
    InlineKeyboardButton("TON",   callback_data="refw_net:TON"),
    InlineKeyboardButton("ERC20", callback_data="refw_net:ERC20"),
    InlineKeyboardButton("SOL",   callback_data="refw_net:SOL"),
)

def ref_withdraw_admin_kb(req_id):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"refw_approve:{req_id}"),
        InlineKeyboardButton("❌ Отклонить",   callback_data=f"refw_reject:{req_id}"),
    )
    return kb

# ── Валидаторы крипто-адресов по сетям (минимальная длина + базовый формат) ──
NETWORK_VALIDATORS = {
    "TRC20": lambda a: bool(re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", a)),
    "ERC20": lambda a: bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", a)),
    "TON"  : lambda a: bool(re.fullmatch(r"[A-Za-z0-9_-]{48,67}", a)),
    "SOL"  : lambda a: bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", a)),
}

# ───────────────────────── УТИЛИТЫ ────────────────────────────────────────
def fmt(value, decimals=2):
    if value >= 1:
        return f"{value:,.{decimals}f}".replace(",", " ")
    return f"{value:.6f}".rstrip("0")

def rates_block(rates, currency):
    icons = {"USDT": "💵", "BTC": "🟠", "TON": "💎", "ETH": "🔷", "SOL": "🟣"}
    lines = [f"  {icons[s]} {s:<5} — {fmt(rates.get(s,0))} {currency}"
             for s in ("USDT","BTC","TON","ETH","SOL")]
    return "\n".join(lines)

def is_admin(uid): return uid == ADMIN_ID

def is_moderator(uid): return uid in moderators

def withdrawal_admin_kb(w_id):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Принять",   callback_data=f"approve:{w_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{w_id}"),
    )
    return kb

def track_user(user):
    today = date.today()
    if stats["today_date"] != today:
        stats["today_date"]   = today
        stats["unique_today"] = set()
    stats["unique_today"].add(user.id)
    stats["all_users"].add(user.id)
    stats["all_users_info"][user.id] = {
        "username": user.username or "",
        "name"    : user.full_name or "",
    }

def is_blocked(uid):
    if uid not in blocked_users: return False
    until = blocked_users[uid]
    if until is None: return True
    if datetime.now() < until: return True
    del blocked_users[uid]
    return False

def track_withdrawal(user_id, username, crypto, amount, payout, currency, country, method, card="—"):
    global _withdrawal_counter
    _withdrawal_counter += 1
    wid = _withdrawal_counter
    stats["withdrawals"].append({
        "id": wid, "user_id": user_id,
        "username": username or f"id{user_id}",
        "crypto": crypto, "amount": amount,
        "payout": payout, "currency": currency,
        "country": country, "method": method,
        "card": card,
        "time": datetime.now().strftime("%d.%m %H:%M"),
        "status": "pending",
    })
    return wid

def get_withdrawal_by_id(wid):
    for w in stats["withdrawals"]:
        if w["id"] == wid: return w
    return None

# ───────────────────────── КУРСЫ ──────────────────────────────────────────
def _fetch_rates():
    ids  = ",".join(COINGECKO_IDS.values())
    curs = ",".join({m[3] for m in COUNTRY_META.values()} | {"usd"})
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ids, "vs_currencies": curs}, timeout=8
    ).json()
    result = {}
    for cur in curs.split(","):
        result[cur] = {}
        for sym, cgid in COINGECKO_IDS.items():
            try:
                base = float(resp[cgid][cur])
            except Exception:
                base = round(FALLBACK_RUB[sym] * FALLBACK_COEF.get(cur, 1.0), 6)
            # применяем ручной коэффициент комиссии если задан
            coef = _manual_rates.get(sym, 1.0)
            result[cur][sym] = round(base * coef, 6)
    return result

def get_rates(currency_code):
    cur = currency_code.lower()
    return _rates_cache.get(
        cur,
        {s: round(FALLBACK_RUB[s] * FALLBACK_COEF.get(cur,1.0), 6) for s in COINGECKO_IDS}
    )

def to_usdt(crypto, amount):
    """USDT-эквивалент суммы обмена (используется для расчёта реферального бонуса)."""
    usd_rate = get_rates("usd").get(crypto, 0)
    return round(amount * usd_rate, 6)

async def rates_updater():
    global _rates_cache
    while True:
        try:
            data = await asyncio.get_event_loop().run_in_executor(None, _fetch_rates)
            _rates_cache = data
            print("[rates] Обновлено")
        except Exception as exc:
            print(f"[rates] Ошибка: {exc}")
        await asyncio.sleep(RATES_TTL)

# ───────────────────────── ВЕБХУК CRYPTOBOT ───────────────────────────────
async def cryptobot_invoice_poller():
    """Периодически проверяем оплаченные инвойсы CryptoBot и уведомляем пользователя."""
    paid_invoices = set()
    while True:
        try:
            invoices = await crypto_pay.get_invoices(status="paid")
            for inv in (invoices.items if hasattr(invoices, "items") else []):
                if inv.invoice_id in paid_invoices:
                    continue
                paid_invoices.add(inv.invoice_id)
                # Ищем заявку по сумме и крипте
                for w in stats["withdrawals"]:
                    if (
                        w.get("method") == "CryptoBot"
                        and w.get("status") == "pending"
                        and str(w.get("crypto")) == str(inv.asset)
                        and abs(float(w.get("amount", 0)) - float(inv.amount)) < 1e-8
                    ):
                        w["status"] = "paid"
                        uid = w["user_id"]
                        try:
                            await bot.send_message(
                                uid,
                                f"✅ <b>Оплата получена!</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"💸 {fmt(w['amount'],6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
                                f"💳 <code>{w['card']}</code>\n\n"
                                f"⏳ Обрабатываем выплату, ожидайте 5–15 минут.\nПо вопросам: {SUPPORT}",
                                parse_mode="HTML")
                        except Exception: pass
                        try:
                            await bot.send_message(
                                ADMIN_ID,
                                f"💰 <b>Оплата CryptoBot — заявка #{w['id']}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"👤 @{w['username']}\n"
                                f"💸 {fmt(w['amount'],6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
                                f"💳 <code>{w['card']}</code>",
                                parse_mode="HTML",
                                reply_markup=withdrawal_admin_kb(w["id"]))
                        except Exception: pass
                        break
        except Exception as exc:
            print(f"[cryptobot_poller] Ошибка: {exc}")
        await asyncio.sleep(15)  # проверяем каждые 15 секунд

# ───────────────────────── ПРОВЕРКА БАНА ──────────────────────────────────
async def check_ban(message):
    uid = message.from_user.id
    if uid == ADMIN_ID or is_moderator(uid): return True
    if is_blocked(uid):
        until = blocked_users.get(uid)
        s = "навсегда" if until is None else f"до {until.strftime('%d.%m.%Y %H:%M')}"
        await message.answer(
            f"🚫 <b>Вы заблокированы</b>\n\nДоступ ограничен {s}.\n\nОбратитесь: {SUPPORT}",
            parse_mode="HTML")
        return False
    return True

# ───────────────────────── ЗАПРОС КАРТЫ ───────────────────────────────────
async def ask_for_card(chat_id, state, wid):
    await CardState.waiting_for_card.set()
    async with state.proxy() as d:
        d["w_id"] = wid
    await bot.send_message(
        chat_id,
        "💳 <b>Укажите реквизиты карты</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Введите номер карты (только цифры):\n"
        "Пример: <code>1234 5678 9123 4567</code>",
        parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove()
    )

# ═══════════════════════════════════════════════════════════════════════════
#  /start  /cancel  /admin
# ═══════════════════════════════════════════════════════════════════════════
@dp.message_handler(commands=["start"], state="*")
async def cmd_start(message: types.Message, state: FSMContext):
    if not await check_ban(message): return
    await state.finish()
    uid    = message.from_user.id
    is_new = uid not in stats["all_users"]
    track_user(message.from_user)

    # ── Реферальная привязка: только при первом запуске, без самореферала ──
    if is_new:
        arg = message.get_args().strip()
        if arg.startswith("ref_"):
            ref_part = arg[4:]
            if ref_part.isdigit():
                ref_id = int(ref_part)
                if ref_id != uid:
                    set_referrer(uid, ref_id, message.from_user.username)

    await message.answer(
        f"💸 <b>VeloxPay</b> — Крипто-обменник\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ Мгновенный вывод криптовалюты на карту\n\n"
        f"🌍 <b>Доступные страны:</b>\n"
        f"  🇷🇺 Россия\n  🇰🇿 Казахстан\n  🇺🇦 Украина\n  🇵🇱 Польша\n"
        f"  🇧🇾 Беларусь\n  🇲🇩 Молдова\n  🇷🇴 Румыния\n\n"
        f"⏱ Среднее время: 5–15 минут\n"
        f"🔄 Курсы обновляются каждые {RATES_TTL} сек.\n"
        f"📣 Канал: @VeloxPay\n"
        f"🛠 Поддержка: {SUPPORT}",
        parse_mode="HTML", reply_markup=main_menu)

@dp.message_handler(commands=["cancel"], state="*")
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.finish()
    if is_admin(message.from_user.id):
        await message.answer("❌ Отменено.", reply_markup=admin_menu)
    else:
        await message.answer("🏠 Главное меню", reply_markup=main_menu)

@dp.message_handler(commands=["admin"], state="*")
async def cmd_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Нет доступа.")
        return
    await state.finish()
    await message.answer(
        "🔐 <b>Панель администратора</b>\n━━━━━━━━━━━━━━━━━━━━━━━\nДобро пожаловать, Admin 👋",
        parse_mode="HTML", reply_markup=admin_menu)

# ── Панель модератора ──
@dp.message_handler(commands=["mod"], state="*")
async def cmd_mod(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if not (is_admin(uid) or is_moderator(uid)):
        await message.answer("⛔️ Нет доступа.")
        return
    await state.finish()
    await message.answer(
        "👮 <b>Панель модератора</b>\n━━━━━━━━━━━━━━━━━━━━━━━\nДобро пожаловать 👋",
        parse_mode="HTML", reply_markup=mod_menu)

# ── Назначить модератора (только создатель) ──
@dp.message_handler(commands=["addmod"], state="*")
async def cmd_addmod(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Нет доступа.")
        return
    arg = message.get_args().strip().lstrip("@")
    if not arg:
        if moderators:
            lines = []
            for m_uid in moderators:
                info  = stats["all_users_info"].get(m_uid, {})
                uname = f"@{info['username']}" if info.get("username") else f"id{m_uid}"
                lines.append(f"  • {uname} (<code>{m_uid}</code>)")
            mod_list = "\n".join(lines)
        else:
            mod_list = "  — пока нет модераторов"
        await message.answer(
            "👮 <b>Модераторы</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{mod_list}\n\n"
            "Чтобы назначить:\n<code>/addmod ID</code> или <code>/addmod @username</code>\n"
            "(пользователь должен хотя бы раз написать боту /start)",
            parse_mode="HTML")
        return
    uid = int(arg) if arg.isdigit() else None
    if not uid:
        for u_id, info in stats["all_users_info"].items():
            if info.get("username", "").lower() == arg.lower():
                uid = u_id; break
    if not uid:
        await message.answer("❌ Пользователь не найден. Пусть сначала напишет боту /start."); return
    if uid == ADMIN_ID:
        await message.answer("❌ Это администратор."); return
    if uid in moderators:
        await message.answer("ℹ️ Этот пользователь уже модератор."); return
    moderators.add(uid)
    info  = stats["all_users_info"].get(uid, {})
    uname = f"@{info['username']}" if info.get("username") else f"id{uid}"
    await message.answer(f"✅ {uname} назначен модератором.")
    try:
        await bot.send_message(
            uid,
            "👮 <b>Вы назначены модератором!</b>\n\nОткройте панель модератора командой /mod",
            parse_mode="HTML")
    except Exception: pass

# ── Снять модератора (только создатель) ──
@dp.message_handler(commands=["delmod"], state="*")
async def cmd_delmod(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Нет доступа.")
        return
    arg = message.get_args().strip().lstrip("@")
    if not arg:
        await message.answer(
            "Использование:\n<code>/delmod ID</code> или <code>/delmod @username</code>",
            parse_mode="HTML")
        return
    uid = int(arg) if arg.isdigit() else None
    if not uid:
        for u_id, info in stats["all_users_info"].items():
            if info.get("username", "").lower() == arg.lower():
                uid = u_id; break
    if not uid or uid not in moderators:
        await message.answer("❌ Этот пользователь не модератор."); return
    moderators.discard(uid)
    info  = stats["all_users_info"].get(uid, {})
    uname = f"@{info['username']}" if info.get("username") else f"id{uid}"
    await message.answer(f"✅ {uname} больше не модератор.")
    try:
        await bot.send_message(uid, "ℹ️ Вы больше не модератор бота.", parse_mode="HTML")
    except Exception: pass

# ═══════════════════════════════════════════════════════════════════════════
#  ГЛАВНОЕ МЕНЮ пользователя
# ═══════════════════════════════════════════════════════════════════════════
@dp.message_handler(lambda m: m.text == "⬅️ Назад", state="*")
async def btn_back(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("🏠 Главное меню", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "💸 Обменять крипту", state="*")
async def btn_exchange(message: types.Message, state: FSMContext):
    if not await check_ban(message): return
    await state.finish()
    track_user(message.from_user)
    await message.answer(
        "🌍 <b>Выберите страну выплаты</b>\n\nКурс будет показан в валюте страны.",
        parse_mode="HTML", reply_markup=countries_menu)

@dp.message_handler(lambda m: m.text == "📜 История операций", state="*")
async def btn_history(message: types.Message):
    if not await check_ban(message): return
    await message.answer("📜 <b>История операций</b>\n━━━━━━━━━━━━━━━━━━\nОперации отсутствуют.", parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "❓ FAQ", state="*")
async def btn_faq(message: types.Message):
    if not await check_ban(message): return
    await message.answer(
        f"❓ <b>FAQ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"💰 Минимальная сумма: {MIN_RUB} RUB\n"
        f"⏱ Время выплаты: 5–15 мин\n"
        f"🕐 Режим работы: 24/7\n"
        f"📣 Канал: @VeloxPay\n",
        parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "🛠 Поддержка 24/7", state="*")
async def btn_support(message: types.Message):
    if not await check_ban(message): return
    await message.answer(f"🛠 <b>Поддержка</b>\n━━━━━━━━━━━━━━━━━━\n{SUPPORT}", parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════════════
#  ОБМЕН — выбор страны / крипты / ввод суммы
# ═══════════════════════════════════════════════════════════════════════════
@dp.message_handler(lambda m: m.text in COUNTRY_META, state="*")
async def country_selected(message: types.Message, state: FSMContext):
    if not await check_ban(message): return
    await state.finish()
    track_user(message.from_user)
    country = message.text
    flag, currency, _, cg_cur = COUNTRY_META[country]
    user_data[message.from_user.id] = {"country": country, "currency": currency, "cg_cur": cg_cur}
    rates = get_rates(cg_cur)
    await message.answer(
        f"{flag} Страна: <b>{country}</b>   💰 Валюта: {currency}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Актуальные курсы:</b>\n\n{rates_block(rates, currency)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n👇 Выберите криптовалюту:",
        parse_mode="HTML", reply_markup=crypto_menu)

@dp.message_handler(lambda m: m.text in ("USDT","BTC","TON","ETH","SOL"), state="*")
async def crypto_selected(message: types.Message, state: FSMContext):
    if not await check_ban(message): return
    uid = message.from_user.id
    if uid not in user_data or "country" not in user_data[uid]:
        await message.answer("⚠️ Сначала выберите страну.", reply_markup=main_menu)
        return
    crypto   = message.text
    cg_cur   = user_data[uid]["cg_cur"]
    currency = user_data[uid]["currency"]
    rates    = get_rates(cg_cur)
    price    = rates.get(crypto, 0)
    user_data[uid]["crypto"] = crypto
    rub_rate   = get_rates("rub").get(crypto, 1)
    min_crypto = MIN_RUB / rub_rate if rub_rate else 0.001
    await ExchangeState.waiting_for_amount.set()
    await message.answer(
        f"✅ Выбрана: <b>{crypto}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Курс: 1 {crypto} = {fmt(price)} {currency}\n"
        f"💰 Минимум: ≈ {fmt(min_crypto, 6)} {crypto}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✍️ Введите количество {crypto}:",
        parse_mode="HTML")

@dp.message_handler(state=ExchangeState.waiting_for_amount)
async def amount_handler(message: types.Message, state: FSMContext):
    if not await check_ban(message): return
    uid = message.from_user.id
    if uid not in user_data or "crypto" not in user_data.get(uid, {}):
        await state.finish()
        return
    crypto   = user_data[uid]["crypto"]
    cg_cur   = user_data[uid]["cg_cur"]
    currency = user_data[uid]["currency"]
    country  = user_data[uid]["country"]
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число.")
        return
    rates      = get_rates(cg_cur)
    price      = rates.get(crypto, 0)
    payout     = amount * price
    payout_rub = amount * get_rates("rub").get(crypto, 0)
    if payout_rub < MIN_RUB:
        await message.answer(f"❌ Минимум ~{MIN_RUB} RUB, ваша сумма ~{fmt(payout_rub)} RUB")
        return
    await state.finish()
    user_data[uid]["amount"] = amount
    user_data[uid]["payout"] = payout
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("⚡ CryptoBot", callback_data="cryptobot"),
        InlineKeyboardButton("📥 На адрес",  callback_data="address"),
    )
    flag = COUNTRY_META[country][0]
    await message.answer(
        f"📋 <b>Детали обмена</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 {fmt(amount, 6)} {crypto}\n"
        f"💳 ≈{fmt(payout)} {currency}\n"
        f"📈 1 {crypto} = {fmt(price)} {currency}\n"
        f"{flag} {country}\n\n"
        f"👇 Выберите способ оплаты:",
        parse_mode="HTML", reply_markup=kb)

# ═══════════════════════════════════════════════════════════════════════════
#  ОПЛАТА
# ═══════════════════════════════════════════════════════════════════════════
@dp.callback_query_handler(lambda c: c.data == "cryptobot", state="*")
async def cryptobot_payment(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if is_blocked(uid):
        await callback.answer("🚫 Заблокирован.", show_alert=True); return
    ud = user_data.get(uid, {})
    crypto, amount, currency, payout, country = (
        ud.get("crypto"), ud.get("amount"), ud.get("currency"),
        ud.get("payout"), ud.get("country"))
    wid = track_withdrawal(uid, callback.from_user.username, crypto, amount, payout, currency, country, "CryptoBot")
    try:
        invoice = await crypto_pay.create_invoice(asset=crypto, amount=amount)
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💸 Оплатить", url=invoice.bot_invoice_url))
        await callback.message.answer(
            f"⚡ <b>Счёт создан</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 {fmt(amount,6)} {crypto}\n💳 ≈{fmt(payout)} {currency}\n"
            f"⏰ Действует 10 минут\n\n👇 Нажмите кнопку чтобы оплатить:",
            parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка создания счёта. Попробуйте «На адрес».\n{SUPPORT}")
        print(f"[cryptobot] {e}")
    await callback.answer()
    await ask_for_card(uid, state, wid)

@dp.callback_query_handler(lambda c: c.data == "address", state="*")
async def address_payment(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if is_blocked(uid):
        await callback.answer("🚫 Заблокирован.", show_alert=True); return
    ud = user_data.get(uid, {})
    crypto, amount, currency, payout, country, cg_cur = (
        ud.get("crypto"), ud.get("amount"), ud.get("currency"),
        ud.get("payout"), ud.get("country"), ud.get("cg_cur"))
    address = CRYPTO_ADDRESSES[crypto]
    price   = get_rates(cg_cur).get(crypto, 0)
    flag    = COUNTRY_META[country][0]
    wid = track_withdrawal(uid, callback.from_user.username, crypto, amount, payout, currency, country, "Адрес")
    await callback.message.answer(
        f"📥 <b>Перевод на адрес — {crypto}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{flag} {country}\n\n📬 Адрес кошелька:\n<code>{address}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💸 Сумма: {fmt(amount,6)} {crypto}\n💳 Выплата: ≈{fmt(payout)} {currency}\n"
        f"📈 1 {crypto} = {fmt(price)} {currency}\n\n⏰ Переведите в течение 10 минут.",
        parse_mode="HTML")
    await callback.answer()
    await ask_for_card(uid, state, wid)

# ═══════════════════════════════════════════════════════════════════════════
#  ВВОД КАРТЫ
# ═══════════════════════════════════════════════════════════════════════════
@dp.message_handler(state=CardState.waiting_for_card)
async def card_entered(message: types.Message, state: FSMContext):
    card = message.text.strip().replace(" ", "")
    if not card.isdigit() or not (13 <= len(card) <= 19):
        await message.answer(
            "❌ Неверный формат.\nПример: <code>1234567891234567</code>",
            parse_mode="HTML"); return
    card_fmt = " ".join(card[i:i+4] for i in range(0, len(card), 4))
    data = await state.get_data()
    await state.finish()
    uid      = message.from_user.id
    username = message.from_user.username
    w = get_withdrawal_by_id(data["w_id"])
    if w: w["card"] = card_fmt
    await message.answer(
        f"✅ <b>Реквизиты сохранены!</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 Карта: <code>{card_fmt}</code>\n\n"
        f"⏳ Заявка принята в обработку.\n⏱ Выплата: 5–15 минут.\n\nПо вопросам: {SUPPORT}",
        parse_mode="HTML", reply_markup=main_menu)
    uname = f"@{username}" if username else f"id{uid}"
    if w:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔔 <b>Новая заявка #{w['id']}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 {uname}\n"
                f"💸 {fmt(w['amount'],6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
                f"🌍 {w['country']}  |  {w['method']}\n💳 <code>{card_fmt}</code>",
                parse_mode="HTML", reply_markup=withdrawal_admin_kb(w["id"]))
        except Exception as e:
            print(f"[card] {e}")

# ═══════════════════════════════════════════════════════════════════════════
#  ОДОБРЕНИЕ / ОТКЛОНЕНИЕ заявок
# ═══════════════════════════════════════════════════════════════════════════
@dp.callback_query_handler(lambda c: c.data.startswith("approve:"), state="*")
async def approve_withdrawal(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Нет доступа.", show_alert=True); return
    wid = int(callback.data.split(":")[1])
    w   = get_withdrawal_by_id(wid)
    if not w or w["status"] != "pending":
        await callback.answer("Уже обработана.", show_alert=True); return
    w["status"] = "approved"
    try:
        await bot.send_message(
            w["user_id"],
            f"✅ <b>Заявка принята!</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 {fmt(w['amount'],6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
            f"💳 <code>{w['card']}</code>\n🌍 {w['country']}\n\n"
            f"⏱ Выплата поступит в течение 5–15 минут.\nПо вопросам: {SUPPORT}",
            parse_mode="HTML")
    except Exception: pass

    # ── Реферальное начисление: ТОЛЬКО после одобрения модератором ──
    try:
        referrer_id = get_referrer(w["user_id"])
        if referrer_id:
            exchange_usdt = to_usdt(w["crypto"], w["amount"])
            reward = round(min(exchange_usdt * REF_PERCENT / 100, MAX_REF_REWARD), 2)
            if reward > 0:
                add_reward(referrer_id, w["user_id"], w["id"], round(exchange_usdt, 2), reward)
                try:
                    await bot.send_message(
                        referrer_id,
                        f"🎉 <b>Реферальный бонус!</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 Ваш реферал совершил обмен ≈{fmt(exchange_usdt)} USDT\n"
                        f"💰 Начислено: <b>{fmt(reward)} USDT</b>\n\n"
                        f"👥 Реферальная программа → 💰 Баланс",
                        parse_mode="HTML")
                except Exception: pass
    except Exception as exc:
        print(f"[referral] Ошибка начисления: {exc}")

    await callback.message.edit_text(callback.message.text + "\n\n<b>✅ Принята</b>", parse_mode="HTML")
    await callback.answer("✅ Принята")

@dp.callback_query_handler(lambda c: c.data.startswith("reject:"), state="*")
async def reject_withdrawal(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Нет доступа.", show_alert=True); return
    wid = int(callback.data.split(":")[1])
    w   = get_withdrawal_by_id(wid)
    if not w or w["status"] != "pending":
        await callback.answer("Уже обработана.", show_alert=True); return
    w["status"] = "rejected"
    try:
        await bot.send_message(
            w["user_id"],
            f"❌ <b>Заявка отклонена</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 {fmt(w['amount'],6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
            f"🌍 {w['country']}\n\n⚠️ Обратитесь в поддержку:\n👉 {SUPPORT}",
            parse_mode="HTML")
    except Exception: pass
    await callback.message.edit_text(callback.message.text + "\n\n<b>❌ Отклонена</b>", parse_mode="HTML")
    await callback.answer("❌ Отклонена")

# ═══════════════════════════════════════════════════════════════════════════
#  ПАНЕЛЬ АДМИНА — кнопки
# ═══════════════════════════════════════════════════════════════════════════

# ── Статистика ──
@dp.message_handler(
    lambda m: m.text == "📊 Статистика" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_stats(message: types.Message):
    today   = date.today()
    w_today = [w for w in stats["withdrawals"] if w["time"].startswith(today.strftime("%d.%m"))]
    pending = [w for w in stats["withdrawals"] if w["status"] == "pending"]
    approved= [w for w in stats["withdrawals"] if w["status"] == "approved"]
    await message.answer(
        f"📊 <b>Статистика</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {today.strftime('%d.%m.%Y')}\n\n"
        f"👥 Сегодня уникальных: <b>{len(stats['unique_today'])}</b>\n"
        f"👤 Всего за всё время: <b>{len(stats['all_users'])}</b>\n\n"
        f"📋 Заявок сегодня: <b>{len(w_today)}</b>\n"
        f"📋 Всего заявок: <b>{len(stats['withdrawals'])}</b>\n"
        f"⏳ Ожидают: <b>{len(pending)}</b>\n"
        f"✅ Выполнено: <b>{len(approved)}</b>\n"
        f"🚫 Заблокировано: <b>{len(blocked_users)}</b>",
        parse_mode="HTML")

# ── Пользователи ──
@dp.message_handler(
    lambda m: m.text == "👥 Пользователи" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_users(message: types.Message):
    lines = []
    for uid in stats["unique_today"]:
        info  = stats["all_users_info"].get(uid, {})
        uname = f"@{info['username']}" if info.get("username") else f"<code>{uid}</code>"
        ban   = " 🚫" if is_blocked(uid) else ""
        lines.append(f"  • {uname}{ban}")
    today_block = "\n".join(lines) if lines else "  —"
    await message.answer(
        f"👥 <b>Пользователи сегодня</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{today_block}\n\nВсего за всё время: <b>{len(stats['all_users'])}</b>",
        parse_mode="HTML")

# ── Заявки ──
@dp.message_handler(
    lambda m: m.text == "📋 Заявки" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_withdrawals(message: types.Message):
    if not stats["withdrawals"]:
        await message.answer("📋 Заявок пока нет."); return
    last = stats["withdrawals"][-10:][::-1]
    status_label = {"pending": "⏳ Ожидает", "approved": "✅ Принята", "rejected": "❌ Отклонена"}
    await message.answer(
        f"📋 <b>Заявки</b> (последние {len(last)} из {len(stats['withdrawals'])})\n━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML")
    for w in last:
        uname = f"@{w['username']}" if not w["username"].startswith("id") else f"<code>{w['user_id']}</code>"
        text  = (
            f"<b>#{w['id']}</b> — {w['time']}\n"
            f"👤 {uname}\n"
            f"💸 {fmt(w['amount'],6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
            f"💳 <code>{w['card']}</code>\n"
            f"🌍 {w['country']}  |  {w['method']}\n"
            f"{status_label.get(w['status'],'—')}"
        )
        kb = withdrawal_admin_kb(w["id"]) if w["status"] == "pending" else None
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

# ── Рассылка ──
@dp.message_handler(
    lambda m: m.text == "📣 Рассылка" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_broadcast_start(message: types.Message, state: FSMContext):
    await BroadcastState.waiting_for_text.set()
    await message.answer(
        f"📣 Рассылка на <b>{len(stats['all_users'])}</b> пользователей.\n\n"
        f"Напишите текст (HTML поддерживается):\n/cancel — отмена",
        parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=BroadcastState.waiting_for_text, content_types=types.ContentTypes.TEXT)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    await state.finish()
    text = message.text
    sent = failed = 0
    for uid in list(stats["all_users"]):
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Отправлено: {sent}\n❌ Ошибок: {failed}", reply_markup=admin_menu)

# ── Написать конкретному юзеру ──
@dp.message_handler(
    lambda m: m.text == "✉️ Написать юзеру" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_msg_user_start(message: types.Message, state: FSMContext):
    await AdminMessageState.waiting_for_user_id.set()
    await message.answer(
        "✉️ Введите ID или @username пользователя:\n/cancel — отмена",
        reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=AdminMessageState.waiting_for_user_id)
async def admin_msg_user_id(message: types.Message, state: FSMContext):
    text = message.text.strip().lstrip("@")
    uid  = int(text) if text.isdigit() else None
    if not uid:
        for u_id, info in stats["all_users_info"].items():
            if info.get("username","").lower() == text.lower():
                uid = u_id; break
    if not uid:
        await message.answer("❌ Пользователь не найден."); return
    async with state.proxy() as d:
        d["target_uid"] = uid
    await AdminMessageState.waiting_for_text.set()
    await message.answer(f"✍️ Теперь напишите текст сообщения для <code>{uid}</code>:\n/cancel — отмена", parse_mode="HTML")

@dp.message_handler(state=AdminMessageState.waiting_for_text, content_types=types.ContentTypes.TEXT)
async def admin_msg_user_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid  = data["target_uid"]
    await state.finish()
    try:
        await bot.send_message(uid, f"📩 <b>Сообщение от поддержки:</b>\n\n{message.text}", parse_mode="HTML")
        await message.answer("✅ Сообщение отправлено.", reply_markup=admin_menu)
    except Exception:
        await message.answer("❌ Не удалось отправить. Возможно юзер заблокировал бота.", reply_markup=admin_menu)

# ── Курс / комиссия ──
@dp.message_handler(
    lambda m: m.text == "💹 Курс/комиссия" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_rate_start(message: types.Message, state: FSMContext):
    lines = []
    for sym in ("USDT","BTC","TON","ETH","SOL"):
        coef = _manual_rates.get(sym, 1.0)
        pct  = round((coef - 1.0) * 100, 2)
        sign = "+" if pct >= 0 else ""
        lines.append(f"  {sym}: {sign}{pct}%")
    await SetRateState.waiting_for_crypto.set()
    await message.answer(
        f"💹 <b>Текущие коэффициенты комиссии:</b>\n\n" + "\n".join(lines) +
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Введите символ крипты для изменения (USDT / BTC / TON / ETH / SOL):\n/cancel — отмена",
        parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=SetRateState.waiting_for_crypto)
async def admin_rate_crypto(message: types.Message, state: FSMContext):
    sym = message.text.strip().upper()
    if sym not in COINGECKO_IDS:
        await message.answer("❌ Неизвестная крипта. Введите: USDT / BTC / TON / ETH / SOL"); return
    async with state.proxy() as d:
        d["rate_sym"] = sym
    await SetRateState.waiting_for_value.set()
    cur = _manual_rates.get(sym, 1.0)
    pct = round((cur - 1.0) * 100, 2)
    await message.answer(
        f"Текущий коэффициент {sym}: <b>{pct:+.2f}%</b>\n\n"
        f"Введите новую комиссию в процентах (например <code>2</code> = +2%, <code>-1</code> = скидка 1%):",
        parse_mode="HTML")

@dp.message_handler(state=SetRateState.waiting_for_value)
async def admin_rate_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    sym  = data["rate_sym"]
    try:
        pct  = float(message.text.replace(",","."))
        coef = 1.0 + pct / 100.0
    except ValueError:
        await message.answer("❌ Введите число."); return
    _manual_rates[sym] = coef
    await state.finish()
    await message.answer(
        f"✅ Комиссия для <b>{sym}</b> установлена: <b>{pct:+.2f}%</b>\n"
        f"Новый коэффициент: {coef:.4f}\n\n"
        f"Курсы обновятся в течение {RATES_TTL} сек.",
        parse_mode="HTML", reply_markup=admin_menu)

# ── Топ крипты ──
@dp.message_handler(
    lambda m: m.text == "📈 Топ крипты" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_top_crypto(message: types.Message):
    if not stats["withdrawals"]:
        await message.answer("📈 Заявок пока нет."); return
    counter = {}
    for w in stats["withdrawals"]:
        counter[w["crypto"]] = counter.get(w["crypto"], 0) + 1
    sorted_crypto = sorted(counter.items(), key=lambda x: x[1], reverse=True)
    icons = {"USDT":"💵","BTC":"🟠","TON":"💎","ETH":"🔷","SOL":"🟣"}
    lines = [f"  {icons.get(s,'🔹')} {s}: <b>{c}</b> заявок" for s,c in sorted_crypto]
    await message.answer(
        "📈 <b>Топ крипты по заявкам</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines),
        parse_mode="HTML")

# ── Блокировка ──
@dp.message_handler(
    lambda m: m.text == "🚫 Заблокировать" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_block_start(message: types.Message, state: FSMContext):
    await BlockState.waiting_for_user_id.set()
    await message.answer("🚫 Введите ID или @username:", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=BlockState.waiting_for_user_id)
async def admin_block_get_user(message: types.Message, state: FSMContext):
    text = message.text.strip().lstrip("@")
    uid  = int(text) if text.isdigit() else None
    if not uid:
        for u_id, info in stats["all_users_info"].items():
            if info.get("username","").lower() == text.lower():
                uid = u_id; break
    if not uid or uid == ADMIN_ID:
        await message.answer("❌ Не найден или это администратор."); return
    async with state.proxy() as d:
        d["block_uid"] = uid
    await BlockState.waiting_for_duration.set()
    await message.answer("⏱ Выберите срок:", reply_markup=block_duration_kb)

@dp.callback_query_handler(lambda c: c.data.startswith("ban_"), state=BlockState.waiting_for_duration)
async def admin_block_duration(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid  = data["block_uid"]
    duration_map = {
        "ban_1h": timedelta(hours=1), "ban_24h": timedelta(hours=24),
        "ban_7d": timedelta(days=7),  "ban_30d": timedelta(days=30),
        "ban_forever": None,
    }
    delta = duration_map.get(callback.data)
    until = None if delta is None else datetime.now() + delta
    blocked_users[uid] = until
    await state.finish()
    info      = stats["all_users_info"].get(uid, {})
    uname     = f"@{info['username']}" if info.get("username") else f"id{uid}"
    until_str = "навсегда" if until is None else f"до {until.strftime('%d.%m %H:%M')}"
    await callback.message.edit_text(f"🚫 {uname} заблокирован {until_str}.")
    await callback.answer()
    await callback.message.answer("Готово.", reply_markup=admin_menu)
    try:
        await bot.send_message(uid, f"🚫 <b>Вы заблокированы</b> {until_str}.\n\nОбратитесь: {SUPPORT}", parse_mode="HTML")
    except Exception: pass

# ── Разблокировка ──
@dp.message_handler(
    lambda m: m.text == "✅ Разблокировать" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_unblock_start(message: types.Message, state: FSMContext):
    if not blocked_users:
        await message.answer("ℹ️ Нет заблокированных."); return
    await UnblockState.waiting_for_user_id.set()
    lines = []
    for uid in blocked_users:
        info  = stats["all_users_info"].get(uid, {})
        uname = f"@{info['username']}" if info.get("username") else f"id{uid}"
        lines.append(f"  • {uname}")
    await message.answer(
        "✅ <b>Разблокировка</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n" +
        "\n".join(lines) + "\n\nВведите ID или @username:",
        parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=UnblockState.waiting_for_user_id)
async def admin_unblock_do(message: types.Message, state: FSMContext):
    text = message.text.strip().lstrip("@")
    uid  = int(text) if text.isdigit() else None
    if not uid:
        for u_id, info in stats["all_users_info"].items():
            if info.get("username","").lower() == text.lower():
                uid = u_id; break
    if not uid or uid not in blocked_users:
        await message.answer("❌ Не найден."); return
    del blocked_users[uid]
    await state.finish()
    info  = stats["all_users_info"].get(uid, {})
    uname = f"@{info['username']}" if info.get("username") else f"id{uid}"
    await message.answer(f"✅ {uname} разблокирован.", reply_markup=admin_menu)
    try:
        await bot.send_message(uid, "✅ <b>Вы разблокированы!</b>\n\nМожете снова пользоваться ботом.", parse_mode="HTML")
    except Exception: pass

# ── Выход из панели ──
@dp.message_handler(
    lambda m: m.text == "🔙 Выйти из панели" and m.from_user.id == ADMIN_ID,
    state="*"
)
async def admin_exit(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("🏠 Главное меню", reply_markup=main_menu)

# ═══════════════════════════════════════════════════════════════════════════
#  ПАНЕЛЬ МОДЕРАТОРА — кнопки
# ═══════════════════════════════════════════════════════════════════════════

# ── Заявки (только просмотр, без кнопок принять/отклонить) ──
@dp.message_handler(
    lambda m: m.text == "📋 Заявки" and is_moderator(m.from_user.id),
    state="*"
)
async def mod_withdrawals(message: types.Message):
    if not stats["withdrawals"]:
        await message.answer("📋 Заявок пока нет."); return
    last = stats["withdrawals"][-10:][::-1]
    status_label = {"pending": "⏳ Ожидает", "approved": "✅ Принята", "rejected": "❌ Отклонена"}
    await message.answer(
        f"📋 <b>Заявки</b> (последние {len(last)} из {len(stats['withdrawals'])})\n━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML")
    for w in last:
        uname = f"@{w['username']}" if not w["username"].startswith("id") else f"<code>{w['user_id']}</code>"
        text  = (
            f"<b>#{w['id']}</b> — {w['time']}\n"
            f"👤 {uname}\n"
            f"💸 {fmt(w['amount'],6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
            f"💳 <code>{w['card']}</code>\n"
            f"🌍 {w['country']}  |  {w['method']}\n"
            f"{status_label.get(w['status'],'—')}"
        )
        await message.answer(text, parse_mode="HTML")

# ── Блокировка (модератор не может заблокировать админа или другого модератора) ──
@dp.message_handler(
    lambda m: m.text == "🚫 Заблокировать" and is_moderator(m.from_user.id),
    state="*"
)
async def mod_block_start(message: types.Message, state: FSMContext):
    await ModBlockState.waiting_for_user_id.set()
    await message.answer("🚫 Введите ID или @username:", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=ModBlockState.waiting_for_user_id)
async def mod_block_get_user(message: types.Message, state: FSMContext):
    text = message.text.strip().lstrip("@")
    uid  = int(text) if text.isdigit() else None
    if not uid:
        for u_id, info in stats["all_users_info"].items():
            if info.get("username","").lower() == text.lower():
                uid = u_id; break
    if not uid:
        await message.answer("❌ Пользователь не найден."); return
    if uid == ADMIN_ID:
        await message.answer("⛔️ Нельзя заблокировать администратора."); return
    if is_moderator(uid):
        await message.answer("⛔️ Нельзя заблокировать другого модератора."); return
    async with state.proxy() as d:
        d["block_uid"] = uid
    await ModBlockState.waiting_for_duration.set()
    await message.answer("⏱ Выберите срок:", reply_markup=block_duration_kb)

@dp.callback_query_handler(lambda c: c.data.startswith("ban_"), state=ModBlockState.waiting_for_duration)
async def mod_block_duration(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid  = data["block_uid"]
    duration_map = {
        "ban_1h": timedelta(hours=1), "ban_24h": timedelta(hours=24),
        "ban_7d": timedelta(days=7),  "ban_30d": timedelta(days=30),
        "ban_forever": None,
    }
    delta = duration_map.get(callback.data)
    until = None if delta is None else datetime.now() + delta
    blocked_users[uid] = until
    await state.finish()
    info      = stats["all_users_info"].get(uid, {})
    uname     = f"@{info['username']}" if info.get("username") else f"id{uid}"
    until_str = "навсегда" if until is None else f"до {until.strftime('%d.%m %H:%M')}"
    await callback.message.edit_text(f"🚫 {uname} заблокирован {until_str}.")
    await callback.answer()
    await callback.message.answer("Готово.", reply_markup=mod_menu)
    try:
        await bot.send_message(uid, f"🚫 <b>Вы заблокированы</b> {until_str}.\n\nОбратитесь: {SUPPORT}", parse_mode="HTML")
    except Exception: pass

# ── Выход из панели модератора ──
@dp.message_handler(
    lambda m: m.text == "🔙 Выйти из панели" and is_moderator(m.from_user.id),
    state="*"
)
async def mod_exit(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("🏠 Главное меню", reply_markup=main_menu)

# ═══════════════════════════════════════════════════════════════════════════
#  РЕФЕРАЛЬНАЯ ПРОГРАММА
# ═══════════════════════════════════════════════════════════════════════════
def ref_overview_text(uid):
    bal     = get_balance(uid)
    invited = get_invited_count(uid)
    active  = get_active_count(uid)
    return (
        f"👥 <b>Реферальная программа</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Приглашено: <b>{invited}</b>\n"
        f"✅ Активных: <b>{active}</b>\n"
        f"💸 Заработано: <b>{fmt(bal['earned'])} USDT</b>\n"
        f"💰 К выводу: <b>{fmt(bal['available'])} USDT</b>"
    )

@dp.message_handler(lambda m: m.text == "👥 Реферальная программа", state="*")
async def ref_open(message: types.Message, state: FSMContext):
    if not await check_ban(message): return
    await state.finish()
    track_user(message.from_user)
    await message.answer(
        ref_overview_text(message.from_user.id) +
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━\n👇 Выберите раздел:",
        parse_mode="HTML", reply_markup=ref_menu)

@dp.message_handler(lambda m: m.text == "🔗 Моя ссылка", state="*")
async def ref_link(message: types.Message):
    if not await check_ban(message): return
    uid  = message.from_user.id
    link = f"https://t.me/VeloxPayExchange_bot?start=ref_{uid}"
    await message.answer(
        f"🔗 <b>Ваша реферальная ссылка</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{link}</code>\n\n"
        f"За каждый завершённый обмен приглашённого вы получаете "
        f"<b>{REF_PERCENT}%</b> от суммы его обмена "
        f"(максимум {fmt(MAX_REF_REWARD)} USDT за заявку).",
        parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "👥 Приглашённые", state="*")
async def ref_invited(message: types.Message):
    if not await check_ban(message): return
    uid  = message.from_user.id
    refs = get_referred_list(uid)
    if not refs:
        await message.answer("👥 У вас пока нет приглашённых пользователей."); return
    active_ids = {r["referred_id"] for r in get_rewards_history(uid, limit=10_000)}
    lines = []
    for r in refs[:30]:
        uname  = f"@{r['username']}" if r.get("username") else f"id{r['user_id']}"
        status = "✅ активен" if r["user_id"] in active_ids else "💤 без обменов"
        lines.append(f"  • {uname} — {status}")
    await message.answer(
        f"👥 <b>Приглашённые</b> ({len(refs)})\n━━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lines),
        parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "📊 Статистика", state="*")
async def ref_stats(message: types.Message):
    if not await check_ban(message): return
    await message.answer(ref_overview_text(message.from_user.id), parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "💰 Баланс", state="*")
async def ref_balance(message: types.Message):
    if not await check_ban(message): return
    bal = get_balance(message.from_user.id)
    await message.answer(
        f"💰 <b>Баланс рефералки</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💸 Заработано всего: {fmt(bal['earned'])} USDT\n"
        f"📤 Выведено: {fmt(bal['withdrawn'])} USDT\n"
        f"⏳ В обработке: {fmt(bal['pending'])} USDT\n"
        f"💰 Доступно к выводу: <b>{fmt(bal['available'])} USDT</b>\n\n"
        f"Минимальный вывод: {fmt(MIN_REF_WITHDRAW)} USDT",
        parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "📜 История", state="*")
async def ref_history(message: types.Message):
    if not await check_ban(message): return
    uid     = message.from_user.id
    rewards = get_rewards_history(uid, limit=10)
    payouts = get_withdraw_history(uid, limit=10)
    if not rewards and not payouts:
        await message.answer("📜 История пуста."); return
    lines = ["📜 <b>История рефералки</b>", "━━━━━━━━━━━━━━━━━━━━━━━", ""]
    if rewards:
        lines.append("<b>Начисления:</b>")
        for r in rewards:
            lines.append(f"  + {fmt(r['reward_amount'])} USDT — {r['created_at']}")
        lines.append("")
    if payouts:
        lines.append("<b>Выводы:</b>")
        status_label = {"approved": "✅ выплачено", "rejected": "❌ отклонено", "pending": "⏳ в обработке"}
        for p in payouts:
            lines.append(f"  − {fmt(p['amount'])} USDT — {status_label.get(p['status'], p['status'])} — {p['created_at']}")
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "💳 Вывести бонус", state="*")
async def ref_withdraw_start(message: types.Message, state: FSMContext):
    if not await check_ban(message): return
    uid = message.from_user.id
    if has_pending_request(uid):
        await message.answer("⏳ У вас уже есть заявка на вывод в обработке. Дождитесь её рассмотрения.")
        return
    bal = get_balance(uid)
    if bal["available"] < MIN_REF_WITHDRAW:
        await message.answer(
            f"❌ Недостаточно средств.\n"
            f"💰 Доступно: {fmt(bal['available'])} USDT\n"
            f"Минимальный вывод: {fmt(MIN_REF_WITHDRAW)} USDT")
        return
    await state.finish()
    async with state.proxy() as d:
        d["ref_withdraw_amount"] = bal["available"]
    await message.answer(
        f"💳 <b>Вывод бонуса</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Сумма к выводу: <b>{fmt(bal['available'])} USDT</b>\n\n"
        f"👇 Выберите способ вывода:",
        parse_mode="HTML", reply_markup=ref_withdraw_method_kb)

@dp.callback_query_handler(lambda c: c.data.startswith("refw_method:"), state="*")
async def ref_withdraw_method(callback: types.CallbackQuery, state: FSMContext):
    method = callback.data.split(":")[1]
    data   = await state.get_data()
    if "ref_withdraw_amount" not in data:
        await callback.answer("⚠️ Сессия устарела, начните заново.", show_alert=True); return
    async with state.proxy() as d:
        d["ref_method"] = method
    await callback.answer()
    if method == "card":
        await RefWithdrawState.waiting_card.set()
        await callback.message.edit_text(
            "💳 <b>Введите номер карты</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Формат: ровно 16 цифр, без пробелов и букв.\n"
            "Пример: <code>5375411234567890</code>",
            parse_mode="HTML")
    else:
        await callback.message.edit_text("🪙 <b>Выберите сеть кошелька</b>", parse_mode="HTML")
        await callback.message.answer("👇", reply_markup=ref_network_kb)

@dp.callback_query_handler(lambda c: c.data.startswith("refw_net:"), state="*")
async def ref_withdraw_network(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "ref_withdraw_amount" not in data:
        await callback.answer("⚠️ Сессия устарела, начните заново.", show_alert=True); return
    network = callback.data.split(":")[1]
    async with state.proxy() as d:
        d["ref_network"] = network
    await RefWithdrawState.waiting_address.set()
    await callback.answer()
    await callback.message.edit_text(
        f"🪙 Сеть: <b>{network}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\nВведите адрес кошелька:",
        parse_mode="HTML")

@dp.message_handler(state=RefWithdrawState.waiting_card)
async def ref_withdraw_card_entered(message: types.Message, state: FSMContext):
    card = message.text.strip().replace(" ", "")
    if not card.isdigit() or len(card) != 16:
        await message.answer(
            "❌ Неверный формат. Нужно ровно 16 цифр, без пробелов и букв.\n"
            "Пример: <code>5375411234567890</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    await _ref_withdraw_finish(message, state, "card", None, card, data["ref_withdraw_amount"])

@dp.message_handler(state=RefWithdrawState.waiting_address)
async def ref_withdraw_address_entered(message: types.Message, state: FSMContext):
    address   = message.text.strip()
    data      = await state.get_data()
    network   = data.get("ref_network")
    validator = NETWORK_VALIDATORS.get(network)
    if (not address) or " " in address or not validator or not validator(address):
        await message.answer(
            f"❌ Неверный адрес для сети <b>{network}</b>. Проверьте формат и отправьте ещё раз.",
            parse_mode="HTML")
        return
    await _ref_withdraw_finish(message, state, "crypto", network, address, data["ref_withdraw_amount"])

async def _ref_withdraw_finish(message, state, method, network, requisites, amount):
    uid      = message.from_user.id
    username = message.from_user.username
    await state.finish()
    req_id       = create_withdraw_request(uid, amount, method, network, requisites)
    method_label = "💳 Банковская карта" if method == "card" else f"🪙 Крипта ({network})"
    await message.answer(
        f"✅ <b>Заявка на вывод создана!</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Сумма: {fmt(amount)} USDT\n"
        f"📤 Способ: {method_label}\n\n"
        f"⏳ Ожидайте подтверждения модератором.",
        parse_mode="HTML", reply_markup=ref_menu)
    uname = f"@{username}" if username else f"id{uid}"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆕 <b>Новая заявка на вывод рефералки</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"ID: {uname} (<code>{uid}</code>)\n"
            f"Сумма: <b>{fmt(amount)} USDT</b>\n"
            f"Способ: {method_label}\n"
            f"Реквизиты: <code>{requisites}</code>",
            parse_mode="HTML", reply_markup=ref_withdraw_admin_kb(req_id))
    except Exception as e:
        print(f"[ref_withdraw] {e}")

# ── Модерация вывода реферального бонуса (только админ) ──
@dp.callback_query_handler(lambda c: c.data.startswith("refw_approve:"), state="*")
async def ref_withdraw_approve(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Нет доступа.", show_alert=True); return
    req_id = int(callback.data.split(":")[1])
    req    = get_withdraw_request(req_id)
    if not req or req["status"] != "pending":
        await callback.answer("Уже обработана.", show_alert=True); return
    set_withdraw_request_status(req_id, "approved")
    add_withdraw_history(req_id, req["user_id"], req["amount"], req["method"],
                                 req["network"], req["requisites"], "approved")
    try:
        await bot.send_message(
            req["user_id"],
            f"✅ <b>Вывод бонуса подтверждён!</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 {fmt(req['amount'])} USDT отправлено.\nПо вопросам: {SUPPORT}",
            parse_mode="HTML")
    except Exception: pass
    await callback.message.edit_text(callback.message.text + "\n\n<b>✅ Подтверждена</b>", parse_mode="HTML")
    await callback.answer("✅ Подтверждена")

@dp.callback_query_handler(lambda c: c.data.startswith("refw_reject:"), state="*")
async def ref_withdraw_reject(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Нет доступа.", show_alert=True); return
    req_id = int(callback.data.split(":")[1])
    req    = get_withdraw_request(req_id)
    if not req or req["status"] != "pending":
        await callback.answer("Уже обработана.", show_alert=True); return
    set_withdraw_request_status(req_id, "rejected")
    add_withdraw_history(req_id, req["user_id"], req["amount"], req["method"],
                                 req["network"], req["requisites"], "rejected")
    try:
        await bot.send_message(
            req["user_id"],
            f"❌ <b>Вывод бонуса отклонён</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 {fmt(req['amount'])} USDT возвращены на баланс.\n"
            f"Обратитесь в поддержку: {SUPPORT}",
            parse_mode="HTML")
    except Exception: pass
    await callback.message.edit_text(callback.message.text + "\n\n<b>❌ Отклонена</b>", parse_mode="HTML")
    await callback.answer("❌ Отклонена")

# ═══════════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════════
async def on_startup(_):
    global _rates_cache
    print("⚡ VeloxPay запускается...")
    init_ref_db()
    # Сразу загружаем курсы при старте, чтобы не было fallback в первые секунды
    try:
        _rates_cache = await asyncio.get_event_loop().run_in_executor(None, _fetch_rates)
        print("[rates] Начальные курсы загружены")
    except Exception as exc:
        print(f"[rates] Ошибка при начальной загрузке: {exc}")
    asyncio.create_task(rates_updater())
    asyncio.create_task(cryptobot_invoice_poller())

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
