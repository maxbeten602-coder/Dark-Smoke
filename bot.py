import asyncio
import html
import logging
import os
import sqlite3
import subprocess
import sys

try:
    import aiogram  # noqa: F401
except ImportError:  # сам ставит библиотеку, если её нет
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiogram>=3.7"])

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)


# ---------- настройки ----------
def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
# ===== НАСТРОЙКИ: меняйте только здесь =====
TOKEN_HERE = "8905318418:AAEoxw9Pz0ra215iiu8TtpWb2W1wat8xlTU"  # токен от @BotFather, между кавычками
OWNER_HERE = "8920532333"         # ID владельца (несколько — через запятую)
SELLER_HERE = "Cathoks"           # кому ведёт кнопка «Связаться»
# ===========================================
BOT_TOKEN = os.getenv("BOT_TOKEN") or TOKEN_HERE
OWNER_IDS = {int(x) for x in (os.getenv("OWNER_IDS") or OWNER_HERE).replace(" ", "").split(",") if x}
DEFAULT_SELLER = (os.getenv("DEFAULT_SELLER") or SELLER_HERE).lstrip("@")
# На Railway при подключённом Volume база лежит на постоянном диске
_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = os.getenv("DB_PATH") or (os.path.join(_volume, "shop.db") if _volume else "shop.db")


# ---------- база данных ----------
def db(sql, args=(), one=False, commit=False):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.execute(sql, args)
    if commit:
        con.commit()
        last = cur.lastrowid
        con.close()
        return last
    res = cur.fetchone() if one else cur.fetchall()
    con.close()
    return res


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS positions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            price TEXT NOT NULL DEFAULT '—',
            qty INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '',
            seller TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(position_id) REFERENCES positions(id) ON DELETE CASCADE);
        """
    )
    con.commit()
    con.close()


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return is_owner(uid) or db("SELECT 1 FROM admins WHERE user_id=?", (uid,), one=True) is not None


def esc(t):
    return html.escape(str(t))


# ---------- состояния ----------
class F_(StatesGroup):
    pos_new = State()
    pos_rename = State()
    prod_name = State()
    prod_price = State()
    prod_qty = State()
    prod_desc = State()
    edit = State()
    admin_add = State()


router = Router()


async def show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


def btn(text, data=None, url=None):
    return InlineKeyboardButton(text=text, callback_data=data, url=url)


# ---------- пользовательская часть ----------
def main_kb(uid):
    b = InlineKeyboardBuilder()
    for p in db("SELECT * FROM positions ORDER BY id"):
        b.row(btn(p["name"], f"pos:{p['id']}"))
    if is_admin(uid):
        b.row(btn("⚙️ Админ панель", "adm"))
    return b.as_markup()


def main_text():
    has = db("SELECT 1 FROM positions LIMIT 1", one=True)
    return "👋 Выберите позицию:" if has else "👋 Позиций пока нет."


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(main_text(), reply_markup=main_kb(m.from_user.id))


@router.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>")


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, main_text(), main_kb(cb.from_user.id))


@router.callback_query(F.data.startswith("pos:"))
async def user_pos(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    pos = db("SELECT * FROM positions WHERE id=?", (pid,), one=True)
    if not pos:
        return await show(cb, "Позиция не найдена.", main_kb(cb.from_user.id))
    b = InlineKeyboardBuilder()
    prods = db("SELECT * FROM products WHERE position_id=? ORDER BY id", (pid,))
    for p in prods:
        mark = "" if p["qty"] > 0 else " ❌"
        b.row(btn(f"{p['name']} — {p['price']}{mark}", f"prod:{p['id']}"))
    b.row(btn("⬅️ Назад", "menu"))
    text = f"📂 <b>{esc(pos['name'])}</b>\n" + ("Выберите товар:" if prods else "Товаров пока нет.")
    await show(cb, text, b.as_markup())


def product_text(p):
    seller = (p["seller"] or DEFAULT_SELLER).lstrip("@")
    stock = f"{p['qty']} шт." if p["qty"] > 0 else "❌ нет в наличии"
    t = f"🛍 <b>{esc(p['name'])}</b>\n\n💰 Цена: {esc(p['price'])}\n📦 В наличии: {stock}\n"
    if p["description"]:
        t += f"\n📝 {esc(p['description'])}\n"
    t += f"\n👤 Продавец: @{esc(seller)}"
    return t, seller


@router.callback_query(F.data.startswith("prod:"))
async def user_prod(cb: CallbackQuery):
    p = db("SELECT * FROM products WHERE id=?", (int(cb.data.split(":")[1]),), one=True)
    if not p:
        return await show(cb, "Товар не найден.", main_kb(cb.from_user.id))
    text, seller = product_text(p)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("💬 Связаться", url=f"https://t.me/{seller}")],
            [btn("⬅️ Назад", f"pos:{p['position_id']}")],
        ]
    )
    await show(cb, text, kb)


# ---------- админ панель ----------
def admin_only(cb: CallbackQuery):
    return is_admin(cb.from_user.id)


@router.callback_query(F.data == "adm")
async def adm(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb):
        return await cb.answer("Нет доступа", show_alert=True)
    await state.clear()
    b = InlineKeyboardBuilder()
    b.row(btn("➕ Создать позицию", "adm_pos_new"))
    for p in db("SELECT * FROM positions ORDER BY id"):
        b.row(btn(f"✏️ {p['name']}", f"adm_pos:{p['id']}"))
    if is_owner(cb.from_user.id):
        b.row(btn("👥 Админы", "adm_admins"))
    b.row(btn("⬅️ В меню", "menu"))
    await show(cb, "⚙️ <b>Админ панель</b>\nВыберите позицию для настройки:", b.as_markup())


# --- позиции
@router.callback_query(F.data == "adm_pos_new")
async def pos_new(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb):
        return await cb.answer()
    await state.set_state(F_.pos_new)
    await show(cb, "Введите название новой позиции:", InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", "adm")]]))


@router.message(F_.pos_new)
async def pos_new_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id) or not m.text:
        return
    pid = db("INSERT INTO positions(name) VALUES(?)", (m.text.strip(),), commit=True)
    await state.clear()
    await m.answer("✅ Позиция создана.", reply_markup=pos_admin_kb(pid))


def pos_admin_kb(pid):
    b = InlineKeyboardBuilder()
    b.row(btn("➕ Добавить товар", f"adm_prod_new:{pid}"))
    for p in db("SELECT * FROM products WHERE position_id=? ORDER BY id", (pid,)):
        b.row(btn(f"🛍 {p['name']} ({p['qty']} шт.)", f"adm_prod:{p['id']}"))
    b.row(btn("✏️ Переименовать", f"adm_pos_ren:{pid}"), btn("🗑 Удалить позицию", f"adm_pos_del:{pid}"))
    b.row(btn("⬅️ Назад", "adm"))
    return b.as_markup()


@router.callback_query(F.data.startswith("adm_pos:"))
async def pos_admin(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb):
        return await cb.answer()
    await state.clear()
    pid = int(cb.data.split(":")[1])
    pos = db("SELECT * FROM positions WHERE id=?", (pid,), one=True)
    if not pos:
        return await cb.answer("Не найдено", show_alert=True)
    await show(cb, f"📂 <b>{esc(pos['name'])}</b>", pos_admin_kb(pid))


@router.callback_query(F.data.startswith("adm_pos_ren:"))
async def pos_ren(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb):
        return await cb.answer()
    pid = int(cb.data.split(":")[1])
    await state.set_state(F_.pos_rename)
    await state.update_data(pid=pid)
    await show(cb, "Введите новое название позиции:", InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", f"adm_pos:{pid}")]]))


@router.message(F_.pos_rename)
async def pos_ren_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id) or not m.text:
        return
    pid = (await state.get_data())["pid"]
    db("UPDATE positions SET name=? WHERE id=?", (m.text.strip(), pid), commit=True)
    await state.clear()
    await m.answer("✅ Переименовано.", reply_markup=pos_admin_kb(pid))


@router.callback_query(F.data.startswith("adm_pos_del:"))
async def pos_del(cb: CallbackQuery):
    if not admin_only(cb):
        return await cb.answer()
    pid = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[btn("✅ Да, удалить", f"adm_pos_delok:{pid}"), btn("❌ Нет", f"adm_pos:{pid}")]]
    )
    await show(cb, "Удалить позицию вместе со всеми её товарами?", kb)


@router.callback_query(F.data.startswith("adm_pos_delok:"))
async def pos_del_ok(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb):
        return await cb.answer()
    pid = int(cb.data.split(":")[1])
    db("DELETE FROM products WHERE position_id=?", (pid,), commit=True)
    db("DELETE FROM positions WHERE id=?", (pid,), commit=True)
    await cb.answer("Удалено")
    await adm(cb, state)


# --- товары
@router.callback_query(F.data.startswith("adm_prod_new:"))
async def prod_new(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb):
        return await cb.answer()
    pid = int(cb.data.split(":")[1])
    await state.set_state(F_.prod_name)
    await state.update_data(pid=pid)
    await show(cb, "Введите название товара:", InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", f"adm_pos:{pid}")]]))


@router.message(F_.prod_name)
async def prod_name(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id) or not m.text:
        return
    await state.update_data(name=m.text.strip())
    await state.set_state(F_.prod_price)
    await m.answer("Введите цену (например: 150 грн):")


@router.message(F_.prod_price)
async def prod_price(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id) or not m.text:
        return
    await state.update_data(price=m.text.strip())
    await state.set_state(F_.prod_qty)
    await m.answer("Сколько штук доступно? (число)")


@router.message(F_.prod_qty)
async def prod_qty(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id) or not m.text:
        return
    if not m.text.strip().isdigit():
        return await m.answer("Нужно целое число, например 10:")
    await state.update_data(qty=int(m.text.strip()))
    await state.set_state(F_.prod_desc)
    await m.answer("Введите описание (или отправьте «-» чтобы пропустить):")


@router.message(F_.prod_desc)
async def prod_desc(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id) or not m.text:
        return
    d = await state.get_data()
    desc = "" if m.text.strip() == "-" else m.text.strip()
    prod_id = db(
        "INSERT INTO products(position_id,name,price,qty,description,seller) VALUES(?,?,?,?,?,?)",
        (d["pid"], d["name"], d["price"], d["qty"], desc, DEFAULT_SELLER),
        commit=True,
    )
    await state.clear()
    await m.answer("✅ Товар добавлен.", reply_markup=prod_admin_kb(prod_id))


def prod_admin_kb(prod_id):
    p = db("SELECT * FROM products WHERE id=?", (prod_id,), one=True)
    b = InlineKeyboardBuilder()
    b.row(btn("✏️ Название", f"adm_edit:name:{prod_id}"), btn("💰 Цена", f"adm_edit:price:{prod_id}"))
    b.row(btn("📦 Количество", f"adm_edit:qty:{prod_id}"), btn("📝 Описание", f"adm_edit:description:{prod_id}"))
    b.row(btn("👤 Продавец", f"adm_edit:seller:{prod_id}"))
    b.row(btn("🗑 Удалить товар", f"adm_prod_del:{prod_id}"))
    b.row(btn("⬅️ Назад", f"adm_pos:{p['position_id']}"))
    return b.as_markup()


@router.callback_query(F.data.startswith("adm_prod:"))
async def prod_admin(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb):
        return await cb.answer()
    await state.clear()
    p = db("SELECT * FROM products WHERE id=?", (int(cb.data.split(":")[1]),), one=True)
    if not p:
        return await cb.answer("Не найдено", show_alert=True)
    text, _ = product_text(p)
    await show(cb, text, prod_admin_kb(p["id"]))


FIELD_NAMES = {
    "name": "новое название",
    "price": "новую цену",
    "qty": "новое количество (число)",
    "description": "новое описание",
    "seller": "юзернейм продавца (например @Cathoks)",
}


@router.callback_query(F.data.startswith("adm_edit:"))
async def edit_start(cb: CallbackQuery, state: FSMContext):
    if not admin_only(cb):
        return await cb.answer()
    _, field, prod_id = cb.data.split(":")
    await state.set_state(F_.edit)
    await state.update_data(field=field, prod_id=int(prod_id))
    await show(
        cb,
        f"Введите {FIELD_NAMES[field]}:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", f"adm_prod:{prod_id}")]]),
    )


@router.message(F_.edit)
async def edit_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id) or not m.text:
        return
    d = await state.get_data()
    field, prod_id = d["field"], d["prod_id"]
    val = m.text.strip()
    if field == "qty":
        if not val.isdigit():
            return await m.answer("Нужно целое число:")
        val = int(val)
    if field == "seller":
        val = val.lstrip("@")
    # field берётся только из фиксированного списка FIELD_NAMES
    if field not in FIELD_NAMES:
        return
    db(f"UPDATE products SET {field}=? WHERE id=?", (val, prod_id), commit=True)
    await state.clear()
    p = db("SELECT * FROM products WHERE id=?", (prod_id,), one=True)
    text, _ = product_text(p)
    await m.answer("✅ Сохранено.\n\n" + text, reply_markup=prod_admin_kb(prod_id))


@router.callback_query(F.data.startswith("adm_prod_del:"))
async def prod_del(cb: CallbackQuery):
    if not admin_only(cb):
        return await cb.answer()
    prod_id = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[btn("✅ Да, удалить", f"adm_prod_delok:{prod_id}"), btn("❌ Нет", f"adm_prod:{prod_id}")]]
    )
    await show(cb, "Удалить этот товар?", kb)


@router.callback_query(F.data.startswith("adm_prod_delok:"))
async def prod_del_ok(cb: CallbackQuery):
    if not admin_only(cb):
        return await cb.answer()
    prod_id = int(cb.data.split(":")[1])
    p = db("SELECT * FROM products WHERE id=?", (prod_id,), one=True)
    if p:
        db("DELETE FROM products WHERE id=?", (prod_id,), commit=True)
        pos = db("SELECT * FROM positions WHERE id=?", (p["position_id"],), one=True)
        await show(cb, f"📂 <b>{esc(pos['name'])}</b>", pos_admin_kb(pos["id"]))
    else:
        await cb.answer()


# --- управление админами (только владелец)
@router.callback_query(F.data == "adm_admins")
async def admins(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        return await cb.answer("Только для владельца", show_alert=True)
    await state.clear()
    b = InlineKeyboardBuilder()
    b.row(btn("➕ Добавить админа", "adm_admin_add"))
    for a in db("SELECT * FROM admins"):
        b.row(btn(f"🗑 {a['user_id']}", f"adm_admin_del:{a['user_id']}"))
    b.row(btn("⬅️ Назад", "adm"))
    await show(cb, "👥 <b>Админы</b>\nНажмите на ID чтобы убрать админа.", b.as_markup())


@router.callback_query(F.data == "adm_admin_add")
async def admin_add(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        return await cb.answer()
    await state.set_state(F_.admin_add)
    await show(
        cb,
        "Отправьте Telegram ID нового админа (узнать: команда /myid в боте).",
        InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", "adm_admins")]]),
    )


@router.message(F_.admin_add)
async def admin_add_save(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id) or not m.text:
        return
    if not m.text.strip().isdigit():
        return await m.answer("Нужен числовой ID:")
    db("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (int(m.text.strip()),), commit=True)
    await state.clear()
    await m.answer("✅ Админ добавлен. Нажмите /start")


@router.callback_query(F.data.startswith("adm_admin_del:"))
async def admin_del(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        return await cb.answer()
    db("DELETE FROM admins WHERE user_id=?", (int(cb.data.split(":")[1]),), commit=True)
    await admins(cb, state)


# ---------- запуск ----------
async def main():
    if not BOT_TOKEN or "ВСТАВЬ" in BOT_TOKEN:
        raise SystemExit("Впишите токен в строке TOKEN_HERE в начале bot.py")
    if not OWNER_IDS:
        logging.warning("OWNER_IDS не задан: админов не будет. Укажите свой ID в .env (узнать: /myid)")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
