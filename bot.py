import asyncio
import requests
from datetime import datetime, date, timedelta

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
RATES_TTL      = 30   # секунд между обновлениями курсов

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
FALLBACK_COEF = {"rub": 1.0, "kzt": 5.2, "uah": 3.8, "pln": 0.33, "byn": 0.30, "mdl": 1.85, "ron": 0.50}

# ───────────────────────── СОСТОЯНИЯ FSM ──────────────────────────────────
class BroadcastState(StatesGroup):
    waiting_for_text = State()

class CardState(StatesGroup):
    waiting_for_card = State()

class BlockState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_duration = State()

class UnblockState(StatesGroup):
    waiting_for_user_id = State()

# ───────────────────────── ДАННЫЕ ─────────────────────────────────────────
stats = {
    "unique_today" : set(),
    "today_date"   : date.today(),
    "all_users"    : set(),
    "all_users_info": {},
    "withdrawals"  : [],
}
blocked_users       = {}
_withdrawal_counter = 0
_rates_cache        = {}
user_data           = {}

# ───────────────────────── ИНИЦИАЛИЗАЦИЯ AIOGRAM ──────────────────────────
storage    = MemoryStorage()
bot        = Bot(token=TOKEN)
dp         = Dispatcher(bot, storage=storage)
crypto_pay = AioCryptoPay(token=CRYPTO_BOT_API, network=Networks.MAIN_NET)

# ───────────────────────── КЛАВИАТУРЫ ─────────────────────────────────────
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("💸 Обменять крипту"))
main_menu.add(KeyboardButton("📜 История операций"), KeyboardButton("❓ FAQ"))
main_menu.add(KeyboardButton("🛠 Поддержка 24/7"))

admin_menu = ReplyKeyboardMarkup(resize_keyboard=True)
admin_menu.add(KeyboardButton("📊 Статистика"))
admin_menu.add(KeyboardButton("👥 Пользователи"), KeyboardButton("📋 Заявки на вывод"))
admin_menu.add(KeyboardButton("📣 Рассылка"))
admin_menu.add(KeyboardButton("🚫 Заблокировать"), KeyboardButton("✅ Разблокировать"))
admin_menu.add(KeyboardButton("🔙 Выйти из панели"))

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

# ───────────────────────── УТИЛИТЫ ────────────────────────────────────────
def fmt(value, decimals=2):
    if value >= 1:
        return f"{value:,.{decimals}f}".replace(",", " ")
    return f"{value:.6f}".rstrip("0")

def rates_block(rates, currency):
    icons = {"USDT": "💵", "BTC": "🟠", "TON": "💎", "ETH": "🔷", "SOL": "🟣"}
    lines = [f"  {icons[s]} {s:<5} — {fmt(rates.get(s, 0))} {currency}"
             for s in ("USDT", "BTC", "TON", "ETH", "SOL")]
    return "\n".join(lines)

def is_admin(user_id):
    return user_id == ADMIN_ID

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

def is_blocked(user_id):
    if user_id not in blocked_users:
        return False
    until = blocked_users[user_id]
    if until is None:
        return True
    if datetime.now() < until:
        return True
    del blocked_users[user_id]
    return False

def track_withdrawal(user_id, username, crypto, amount, payout, currency, country, method, card="—"):
    global _withdrawal_counter
    _withdrawal_counter += 1
    w_id = _withdrawal_counter
    stats["withdrawals"].append({
        "id"      : w_id,
        "user_id" : user_id,
        "username": username or f"id{user_id}",
        "crypto"  : crypto,
        "amount"  : amount,
        "payout"  : payout,
        "currency": currency,
        "country" : country,
        "method"  : method,
        "card"    : card,
        "time"    : datetime.now().strftime("%d.%m %H:%M"),
        "status"  : "pending",
    })
    return w_id

def get_withdrawal_by_id(w_id):
    for w in stats["withdrawals"]:
        if w["id"] == w_id:
            return w
    return None

# ───────────────────────── КУРСЫ ──────────────────────────────────────────
def _fetch_rates():
    ids        = ",".join(COINGECKO_IDS.values())
    currencies = ",".join({m[3] for m in COUNTRY_META.values()})
    resp = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ids, "vs_currencies": currencies},
        timeout=8
    ).json()
    result = {}
    for cur in currencies.split(","):
        result[cur] = {}
        for symbol, cg_id in COINGECKO_IDS.items():
            try:
                result[cur][symbol] = float(resp[cg_id][cur])
            except Exception:
                result[cur][symbol] = round(
                    FALLBACK_RUB[symbol] * FALLBACK_COEF.get(cur, 1.0), 6
                )
    return result

def get_rates(currency_code):
    cur = currency_code.lower()
    return _rates_cache.get(
        cur,
        {s: round(FALLBACK_RUB[s] * FALLBACK_COEF.get(cur, 1.0), 6) for s in COINGECKO_IDS}
    )

async def rates_updater():
    global _rates_cache
    while True:
        try:
            data        = await asyncio.get_event_loop().run_in_executor(None, _fetch_rates)
            _rates_cache = data
            print("[rates] Обновлено")
        except Exception as exc:
            print(f"[rates] Ошибка: {exc}")
        await asyncio.sleep(RATES_TTL)

# ───────────────────────── ПРОВЕРКА БАНА ──────────────────────────────────
async def check_ban(message):
    uid = message.from_user.id
    if uid == ADMIN_ID:
        return True
    if is_blocked(uid):
        until     = blocked_users.get(uid)
        until_str = "навсегда" if until is None else f"до {until.strftime('%d.%m.%Y %H:%M')}"
        await message.answer(
            f"🚫 <b>Вы заблокированы</b>\n\nДоступ ограничен {until_str}.\n\nОбратитесь: {SUPPORT}",
            parse_mode="HTML"
        )
        return False
    return True

# ───────────────────────── ЗАПРОС КАРТЫ ───────────────────────────────────
async def ask_for_card(chat_id, state, w_id):
    await CardState.waiting_for_card.set()
    async with state.proxy() as data:
        data["w_id"] = w_id
    await bot.send_message(
        chat_id,
        f"💳 <b>Укажите реквизиты карты</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Введите номер карты (только цифры):\n"
        f"Пример: <code>1234 5678 9123 4567</code>",
        parse_mode="HTML",
        reply_markup=types.ReplyKeyboardRemove()
    )

# ───────────────────────── /start ─────────────────────────────────────────
@dp.message_handler(commands=["start"], state="*")
async def start(message: types.Message, state: FSMContext):
    if not await check_ban(message):
        return
    await state.finish()
    track_user(message.from_user)
    await message.answer(
        f"💸 <b>VeloxPay</b> — Крипто-обменник\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ Мгновенный вывод криптовалюты на карту\n\n"
        f"🌍 <b>Доступные страны:</b>\n"
        f"  🇷🇺 Россия\n  🇰🇿 Казахстан\n  🇺🇦 Украина\n  🇵🇱 Польша\n"
        f"  🇧🇾 Беларусь\n  🇲🇩 Молдова\n  🇷🇴 Румыния\n\n"
        f"⏱ Среднее время: 5–15 минут\n"
        f"🔄 Курсы обновляются каждые {RATES_TTL} сек.\n"
        f"📣 Канал: @VeloxExchange\n"
        f"🛠 Поддержка: {SUPPORT}",
        parse_mode="HTML",
        reply_markup=main_menu
    )

# ───────────────────────── /cancel ────────────────────────────────────────
@dp.message_handler(
    commands=["cancel"],
    state=[
        BlockState.waiting_for_user_id, BlockState.waiting_for_duration,
        UnblockState.waiting_for_user_id,
        BroadcastState.waiting_for_text,
        CardState.waiting_for_card,
    ]
)
async def cancel_state(message: types.Message, state: FSMContext):
    await state.finish()
    if is_admin(message.from_user.id):
        await message.answer("❌ Отменено.", reply_markup=admin_menu)
    else:
        await message.answer("🏠 Главное меню", reply_markup=main_menu)

# ───────────────────────── /admin ─────────────────────────────────────────
@dp.message_handler(commands=["admin"], state="*")
async def admin_enter(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Нет доступа.")
        return
    await state.finish()
    await message.answer(
        "🔐 <b>Панель администратора</b>\n━━━━━━━━━━━━━━━━━━━━━━━\nДобро пожаловать, Admin 👋",
        parse_mode="HTML",
        reply_markup=admin_menu
    )

# ───────────────────────── ГЛАВНОЕ МЕНЮ ───────────────────────────────────
@dp.message_handler(lambda m: m.text == "💸 Обменять крипту", state="*")
async def exchange(message: types.Message, state: FSMContext):
    if not await check_ban(message):
        return
    await state.finish()
    track_user(message.from_user)
    await message.answer(
        "🌍 <b>Выберите страну выплаты</b>\n\nКурс будет показан в валюте страны.",
        parse_mode="HTML",
        reply_markup=countries_menu
    )

@dp.message_handler(lambda m: m.text == "📜 История операций", state="*")
async def history(message: types.Message):
    if not await check_ban(message):
        return
    await message.answer("📜 <b>История операций</b>\n━━━━━━━━━━━━━━━━━━\nОперации отсутствуют.", parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "❓ FAQ", state="*")
async def faq(message: types.Message):
    if not await check_ban(message):
        return
    await message.answer(
        f"❓ <b>FAQ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"💰 Минимальная сумма: {MIN_RUB} RUB\n"
        f"⏱ Время выплаты: 5–15 мин\n"
        f"🕐 Режим работы: 24/7\n"
        f"📣 Канал: @VeloxExchange\n"
        f"🛠 Поддержка: {SUPPORT}",
        parse_mode="HTML"
    )

@dp.message_handler(lambda m: m.text == "🛠 Поддержка 24/7", state="*")
async def support(message: types.Message):
    if not await check_ban(message):
        return
    await message.answer(f"🛠 <b>Поддержка</b>\n━━━━━━━━━━━━━━━━━━\n{SUPPORT}", parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "⬅️ Назад", state="*")
async def back(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("🏠 Главное меню", reply_markup=main_menu)

# ───────────────────────── ОБМЕН — СТРАНА ─────────────────────────────────
@dp.message_handler(lambda m: m.text in COUNTRY_META, state="*")
async def country_selected(message: types.Message, state: FSMContext):
    if not await check_ban(message):
        return
    await state.finish()
    track_user(message.from_user)
    country              = message.text
    flag, currency, _, cg_cur = COUNTRY_META[country]
    user_data[message.from_user.id] = {"country": country, "currency": currency, "cg_cur": cg_cur}
    rates = get_rates(cg_cur)
    await message.answer(
        f"{flag} Страна: <b>{country}</b>   💰 Валюта: {currency}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Актуальные курсы:</b>\n\n{rates_block(rates, currency)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n👇 Выберите криптовалюту:",
        parse_mode="HTML",
        reply_markup=crypto_menu
    )

# ───────────────────────── ОБМЕН — КРИПТА ─────────────────────────────────
@dp.message_handler(lambda m: m.text in ("USDT", "BTC", "TON", "ETH", "SOL"), state="*")
async def crypto_selected(message: types.Message, state: FSMContext):
    if not await check_ban(message):
        return
    await state.finish()
    user_id = message.from_user.id
    if user_id not in user_data or "country" not in user_data[user_id]:
        await message.answer("⚠️ Сначала выберите страну.", reply_markup=main_menu)
        return
    crypto   = message.text
    cg_cur   = user_data[user_id]["cg_cur"]
    currency = user_data[user_id]["currency"]
    rates    = get_rates(cg_cur)
    price    = rates.get(crypto, 0)
    user_data[user_id]["crypto"] = crypto
    rub_rate   = get_rates("rub").get(crypto, 1)
    min_crypto = MIN_RUB / rub_rate if rub_rate else 0.001
    await message.answer(
        f"✅ Выбрана: <b>{crypto}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 Курс: 1 {crypto} = {fmt(price)} {currency}\n"
        f"💰 Минимум: ≈ {fmt(min_crypto, 6)} {crypto}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✍️ Введите количество {crypto}:",
        parse_mode="HTML"
    )

# ───────────────────────── ОБМЕН — СУММА ──────────────────────────────────
@dp.message_handler(state=None)
async def amount_handler(message: types.Message):
    if not await check_ban(message):
        return
    user_id = message.from_user.id
    if user_id not in user_data or "crypto" not in user_data.get(user_id, {}):
        return
    crypto   = user_data[user_id]["crypto"]
    cg_cur   = user_data[user_id]["cg_cur"]
    currency = user_data[user_id]["currency"]
    country  = user_data[user_id]["country"]
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        return
    rates       = get_rates(cg_cur)
    price       = rates.get(crypto, 0)
    payout      = amount * price
    rub_rates   = get_rates("rub")
    payout_rub  = amount * rub_rates.get(crypto, 0)
    if payout_rub < MIN_RUB:
        await message.answer(f"❌ Минимум ~{MIN_RUB} RUB, ваша сумма ~{fmt(payout_rub)} RUB")
        return
    user_data[user_id]["amount"] = amount
    user_data[user_id]["payout"] = payout
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("⚡ CryptoBot",   callback_data="cryptobot"),
        InlineKeyboardButton("📥 На адрес", callback_data="address"),
    )
    flag = COUNTRY_META[country][0]
    await message.answer(
        f"📋 <b>Детали обмена</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 {fmt(amount, 6)} {crypto}\n"
        f"💳 ≈{fmt(payout)} {currency}\n"
        f"📈 1 {crypto} = {fmt(price)} {currency}\n"
        f"{flag} {country}\n\n"
        f"👇 Выберите способ оплаты:",
        parse_mode="HTML",
        reply_markup=kb
    )

# ───────────────────────── ОПЛАТА — CRYPTOBOT ─────────────────────────────
@dp.callback_query_handler(lambda c: c.data == "cryptobot")
async def cryptobot_payment(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if is_blocked(user_id):
        await callback.answer("🚫 Заблокирован.", show_alert=True)
        return
    ud       = user_data.get(user_id, {})
    crypto   = ud.get("crypto")
    amount   = ud.get("amount")
    currency = ud.get("currency")
    payout   = ud.get("payout")
    country  = ud.get("country")
    username = callback.from_user.username
    w_id     = track_withdrawal(user_id, username, crypto, amount, payout, currency, country, "CryptoBot")
    try:
        invoice = await crypto_pay.create_invoice(asset=crypto, amount=amount)
        pay_url = invoice.bot_invoice_url
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💸 Оплатить", url=pay_url))
        await callback.message.answer(
            f"⚡ <b>Счёт создан</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 {fmt(amount, 6)} {crypto}\n"
            f"💳 ≈{fmt(payout)} {currency}\n"
            f"⏰ Действует 10 минут\n\n"
            f"👇 Нажмите кнопку чтобы оплатить:",
            parse_mode="HTML",
            reply_markup=kb
        )
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка создания счёта. Попробуйте «На адрес».\n\n{SUPPORT}")
        print(f"[cryptobot] Ошибка: {e}")
    await callback.answer()
    await ask_for_card(callback.from_user.id, state, w_id)

# ───────────────────────── ОПЛАТА — АДРЕС ────────────────────────────────
@dp.callback_query_handler(lambda c: c.data == "address")
async def address_payment(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if is_blocked(user_id):
        await callback.answer("🚫 Заблокирован.", show_alert=True)
        return
    ud       = user_data.get(user_id, {})
    crypto   = ud.get("crypto")
    amount   = ud.get("amount")
    payout   = ud.get("payout")
    currency = ud.get("currency")
    country  = ud.get("country")
    cg_cur   = ud.get("cg_cur")
    address  = CRYPTO_ADDRESSES[crypto]
    price    = get_rates(cg_cur).get(crypto, 0)
    flag     = COUNTRY_META[country][0]
    username = callback.from_user.username
    w_id     = track_withdrawal(user_id, username, crypto, amount, payout, currency, country, "Адрес")
    await callback.message.answer(
        f"📥 <b>Перевод на адрес — {crypto}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{flag} {country}\n\n"
        f"📬 Адрес кошелька:\n<code>{address}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💸 Сумма: {fmt(amount, 6)} {crypto}\n"
        f"💳 Выплата: ≈{fmt(payout)} {currency}\n"
        f"📈 1 {crypto} = {fmt(price)} {currency}\n\n"
        f"⏰ Переведите в течение 10 минут.",
        parse_mode="HTML"
    )
    await callback.answer()
    await ask_for_card(callback.from_user.id, state, w_id)

# ───────────────────────── ВВОД КАРТЫ ────────────────────────────────────
@dp.message_handler(state=CardState.waiting_for_card)
async def card_entered(message: types.Message, state: FSMContext):
    card = message.text.strip().replace(" ", "")
    if not card.isdigit() or not (13 <= len(card) <= 19):
        await message.answer(
            "❌ Неверный формат карты.\n\nВведите только цифры, 13–19 символов.\n"
            "Пример: <code>1234567891234567</code>",
            parse_mode="HTML"
        )
        return
    card_fmt = " ".join(card[i:i+4] for i in range(0, len(card), 4))
    data     = await state.get_data()
    await state.finish()
    user_id  = message.from_user.id
    username = message.from_user.username
    w        = get_withdrawal_by_id(data["w_id"])
    if w:
        w["card"] = card_fmt
    await message.answer(
        f"✅ <b>Реквизиты сохранены!</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 Карта: <code>{card_fmt}</code>\n\n"
        f"⏳ Заявка принята в обработку.\n"
        f"⏱ Выплата: 5–15 минут.\n\n"
        f"По вопросам: {SUPPORT}",
        parse_mode="HTML",
        reply_markup=main_menu
    )
    uname = f"@{username}" if username else f"id{user_id}"
    if w:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔔 <b>Новая заявка #{w['id']}</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 {uname}\n"
                f"💸 {fmt(w['amount'], 6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
                f"🌍 {w['country']}  |  {w['method']}\n"
                f"💳 <code>{card_fmt}</code>",
                parse_mode="HTML",
                reply_markup=withdrawal_admin_kb(w["id"])
            )
        except Exception as e:
            print(f"[card] Не удалось уведомить админа: {e}")

# ───────────────────────── ОДОБРЕНИЕ / ОТКЛОНЕНИЕ ─────────────────────────
@dp.callback_query_handler(lambda c: c.data.startswith("approve:"))
async def approve_withdrawal(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Нет доступа.", show_alert=True)
        return
    w_id = int(callback.data.split(":")[1])
    w    = get_withdrawal_by_id(w_id)
    if not w or w["status"] != "pending":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    w["status"] = "approved"
    try:
        await bot.send_message(
            w["user_id"],
            f"✅ <b>Заявка принята!</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 {fmt(w['amount'], 6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
            f"💳 <code>{w['card']}</code>\n"
            f"🌍 {w['country']}\n\n"
            f"⏱ Выплата поступит в течение 5–15 минут.\n"
            f"По вопросам: {SUPPORT}",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await callback.message.edit_text(callback.message.text + "\n\n<b>✅ Принята</b>", parse_mode="HTML")
    await callback.answer("✅ Заявка принята")

@dp.callback_query_handler(lambda c: c.data.startswith("reject:"))
async def reject_withdrawal(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔️ Нет доступа.", show_alert=True)
        return
    w_id = int(callback.data.split(":")[1])
    w    = get_withdrawal_by_id(w_id)
    if not w or w["status"] != "pending":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    w["status"] = "rejected"
    try:
        await bot.send_message(
            w["user_id"],
            f"❌ <b>Заявка отклонена</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 {fmt(w['amount'], 6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
            f"🌍 {w['country']}\n\n"
            f"⚠️ Обратитесь в поддержку:\n👉 {SUPPORT}",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await callback.message.edit_text(callback.message.text + "\n\n<b>❌ Отклонена</b>", parse_mode="HTML")
    await callback.answer("❌ Заявка отклонена")

# ───────────────────────── ПАНЕЛЬ АДМИНА ──────────────────────────────────
@dp.message_handler(lambda m: m.text == "📊 Статистика" and m.from_user.id == ADMIN_ID, state="*")
async def cmd_stats(message: types.Message):
    today   = date.today()
    w_today = [w for w in stats["withdrawals"] if w["time"].startswith(today.strftime("%d.%m"))]
    pending = [w for w in stats["withdrawals"] if w["status"] == "pending"]
    await message.answer(
        f"📊 <b>Статистика</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 {today.strftime('%d.%m.%Y')}\n\n"
        f"👥 Сегодня уникальных: <b>{len(stats['unique_today'])}</b>\n"
        f"👤 Всего за всё время: <b>{len(stats['all_users'])}</b>\n\n"
        f"📋 Заявок сегодня: <b>{len(w_today)}</b>\n"
        f"📋 Всего заявок: <b>{len(stats['withdrawals'])}</b>\n"
        f"⏳ Ожидают решения: <b>{len(pending)}</b>\n"
        f"🚫 Заблокировано: <b>{len(blocked_users)}</b>",
        parse_mode="HTML"
    )

@dp.message_handler(lambda m: m.text == "👥 Пользователи" and m.from_user.id == ADMIN_ID, state="*")
async def cmd_users(message: types.Message):
    lines = []
    for uid in stats["unique_today"]:
        info  = stats["all_users_info"].get(uid, {})
        uname = f"@{info['username']}" if info.get("username") else f"<code>{uid}</code>"
        ban   = " 🚫" if is_blocked(uid) else ""
        lines.append(f"  • {uname}{ban}")
    today_block = "\n".join(lines) if lines else "  —"
    await message.answer(
        f"👥 <b>Пользователи сегодня</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{today_block}\n\n"
        f"Всего за всё время: <b>{len(stats['all_users'])}</b>",
        parse_mode="HTML"
    )

@dp.message_handler(lambda m: m.text == "📋 Заявки на вывод" and m.from_user.id == ADMIN_ID, state="*")
async def cmd_withdrawals(message: types.Message):
    if not stats["withdrawals"]:
        await message.answer("📋 Заявок пока нет.")
        return
    last         = stats["withdrawals"][-10:][::-1]
    status_label = {"pending": "⏳ Ожидает", "approved": "✅ Принята", "rejected": "❌ Отклонена"}
    await message.answer(
        f"📋 <b>Заявки</b> (последние {len(last)} из {len(stats['withdrawals'])})\n━━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML"
    )
    for w in last:
        uname = f"@{w['username']}" if not w["username"].startswith("id") else f"<code>{w['user_id']}</code>"
        text  = (
            f"<b>#{w['id']}</b> — {w['time']}\n"
            f"👤 {uname}\n"
            f"💸 {fmt(w['amount'], 6)} {w['crypto']} → ≈{fmt(w['payout'])} {w['currency']}\n"
            f"💳 <code>{w['card']}</code>\n"
            f"🌍 {w['country']}  |  {w['method']}\n"
            f"{status_label.get(w['status'], '—')}"
        )
        kb = withdrawal_admin_kb(w["id"]) if w["status"] == "pending" else None
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

# ───────────────────────── БЛОКИРОВКА ─────────────────────────────────────
@dp.message_handler(lambda m: m.text == "🚫 Заблокировать" and m.from_user.id == ADMIN_ID, state="*")
async def block_start(message: types.Message, state: FSMContext):
    await BlockState.waiting_for_user_id.set()
    await message.answer("🚫 Введите ID или @username пользователя:", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=BlockState.waiting_for_user_id)
async def block_get_user(message: types.Message, state: FSMContext):
    text = message.text.strip().lstrip("@")
    uid  = int(text) if text.isdigit() else None
    if not uid:
        for u_id, info in stats["all_users_info"].items():
            if info.get("username", "").lower() == text.lower():
                uid = u_id
                break
    if not uid or uid == ADMIN_ID:
        await message.answer("❌ Пользователь не найден или это администратор.")
        return
    async with state.proxy() as data:
        data["block_uid"] = uid
    await BlockState.waiting_for_duration.set()
    await message.answer("⏱ Выберите срок блокировки:", reply_markup=block_duration_kb)

@dp.callback_query_handler(lambda c: c.data.startswith("ban_"), state=BlockState.waiting_for_duration)
async def block_set_duration(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid  = data["block_uid"]
    duration_map = {
        "ban_1h"     : timedelta(hours=1),
        "ban_24h"    : timedelta(hours=24),
        "ban_7d"     : timedelta(days=7),
        "ban_30d"    : timedelta(days=30),
        "ban_forever": None,
    }
    delta    = duration_map.get(callback.data)
    until    = None if delta is None else datetime.now() + delta
    blocked_users[uid] = until
    await state.finish()
    info      = stats["all_users_info"].get(uid, {})
    uname     = f"@{info['username']}" if info.get("username") else f"id{uid}"
    until_str = "навсегда" if until is None else f"до {until.strftime('%d.%m %H:%M')}"
    await callback.message.edit_text(f"🚫 Пользователь {uname} заблокирован {until_str}.")
    await callback.answer()
    await callback.message.answer("Готово.", reply_markup=admin_menu)
    try:
        await bot.send_message(uid, f"🚫 <b>Вы заблокированы</b> {until_str}.\n\nОбратитесь: {SUPPORT}", parse_mode="HTML")
    except Exception:
        pass

# ───────────────────────── РАЗБЛОКИРОВКА ──────────────────────────────────
@dp.message_handler(lambda m: m.text == "✅ Разблокировать" and m.from_user.id == ADMIN_ID, state="*")
async def unblock_start(message: types.Message, state: FSMContext):
    if not blocked_users:
        await message.answer("ℹ️ Нет заблокированных пользователей.")
        return
    await UnblockState.waiting_for_user_id.set()
    lines = []
    for uid in blocked_users:
        info  = stats["all_users_info"].get(uid, {})
        uname = f"@{info['username']}" if info.get("username") else f"id{uid}"
        lines.append(f"  • {uname}")
    await message.answer(
        f"✅ <b>Разблокировка</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        + "\n".join(lines) +
        "\n\nВведите ID или @username:",
        parse_mode="HTML",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message_handler(state=UnblockState.waiting_for_user_id)
async def unblock_do(message: types.Message, state: FSMContext):
    text = message.text.strip().lstrip("@")
    uid  = int(text) if text.isdigit() else None
    if not uid:
        for u_id, info in stats["all_users_info"].items():
            if info.get("username", "").lower() == text.lower():
                uid = u_id
                break
    if not uid or uid not in blocked_users:
        await message.answer("❌ Пользователь не найден в списке заблокированных.")
        return
    del blocked_users[uid]
    await state.finish()
    info  = stats["all_users_info"].get(uid, {})
    uname = f"@{info['username']}" if info.get("username") else f"id{uid}"
    await message.answer(f"✅ {uname} разблокирован.", reply_markup=admin_menu)
    try:
        await bot.send_message(uid, "✅ <b>Вы разблокированы!</b>\n\nМожете снова пользоваться ботом.", parse_mode="HTML")
    except Exception:
        pass

# ───────────────────────── РАССЫЛКА ───────────────────────────────────────
@dp.message_handler(lambda m: m.text == "📣 Рассылка" and m.from_user.id == ADMIN_ID, state="*")
async def broadcast_start(message: types.Message, state: FSMContext):
    await BroadcastState.waiting_for_text.set()
    await message.answer(
        f"📣 Рассылка на <b>{len(stats['all_users'])}</b> пользователей.\n\n"
        f"Напишите текст сообщения (поддерживается HTML):\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message_handler(state=BroadcastState.waiting_for_text, content_types=types.ContentTypes.TEXT)
async def broadcast_send(message: types.Message, state: FSMContext):
    await state.finish()
    text      = message.text
    all_users = list(stats["all_users"])
    if not all_users:
        await message.answer("⚠️ Нет пользователей для рассылки.", reply_markup=admin_menu)
        return
    sent = failed = 0
    for uid in all_users:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Отправлено: {sent}\n❌ Ошибок: {failed}", reply_markup=admin_menu)

@dp.message_handler(lambda m: m.text == "🔙 Выйти из панели" and m.from_user.id == ADMIN_ID, state="*")
async def admin_exit(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("🏠 Главное меню", reply_markup=main_menu)

# ───────────────────────── ЗАПУСК ─────────────────────────────────────────
async def on_startup(_):
    print("⚡ VeloxPay запускается...")
    asyncio.create_task(rates_updater())

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
