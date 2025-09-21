# bot_aiogram.py
import asyncio
import logging
import random
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext

# ---------------- CONFIG ----------------
BOT_TOKEN = "7212656453:AAEEHziWSN4EhjxqGg2A-nUQUTtopniMnWo"   # <-- put your token here locally
ADMIN_ID = 7781534875                # <-- your Telegram ID (admin)
MANDATORY_CHANNEL = "@uzbek_coder1"  # <-- channel username or chat id required to join
DB_PATH = "bot.db"
LOCAL_TZ_OFFSET = +5  # Tashkent UTC+5 (if you want to store naive datetimes, adjust)
# ----------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# in-memory structures used during running tournaments
user_seen_questions: Dict[int, set] = {}
tournament_tasks: Dict[int, asyncio.Task] = {}
GLOBAL_TOURNAMENT_ANSWERS: Dict[Any, Dict[int, str]] = {}
GLOBAL_DP = dp  # for access in tasks

# ---------------- DB helpers ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        name TEXT,
        phone TEXT,
        daraja INTEGER DEFAULT 0,
        coin INTEGER DEFAULT 0,
        is_pro INTEGER DEFAULT 0,
        refer_count INTEGER DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL,
        correct TEXT NOT NULL,
        opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT,
        for_pro INTEGER DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_iso TEXT,
        topic TEXT,
        prizes TEXT,
        num_questions INTEGER DEFAULT 5
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tournament_participants (
        tournament_id INTEGER,
        tg_id INTEGER,
        PRIMARY KEY(tournament_id, tg_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tournament_scores (
        tournament_id INTEGER,
        tg_id INTEGER,
        correct_count INTEGER DEFAULT 0,
        PRIMARY KEY(tournament_id, tg_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tournament_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT NOT NULL,
        correct TEXT NOT NULL,
        opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT
    )
    """)
    conn.commit()
    conn.close()

def db_get_user(tg_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT tg_id, name, phone, daraja, coin, is_pro, refer_count FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row

def db_add_user(tg_id: int, name: str, phone: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (tg_id, name, phone) VALUES (?, ?, ?)", (tg_id, name, phone))
    conn.commit()
    conn.close()

def db_update_score(tg_id: int, daraja_inc=0, coin_inc=0):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET daraja = daraja + ?, coin = coin + ? WHERE tg_id=?", (daraja_inc, coin_inc, tg_id))
    conn.commit()
    conn.close()

def db_set_pro(tg_id: int, is_pro=1):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_pro = ? WHERE tg_id=?", (is_pro, tg_id))
    conn.commit()
    conn.close()

def db_add_question(q, correct, opts, for_pro=0):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO questions (question, correct, opt1, opt2, opt3, opt4, for_pro) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (q, correct, opts[0], opts[1], opts[2], opts[3], for_pro)
    )
    conn.commit()
    conn.close()

def db_get_random_question(for_pro=0, exclude_ids: Optional[set]=None):
    exclude_ids = exclude_ids or set()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, question, correct, opt1, opt2, opt3, opt4 FROM questions WHERE for_pro=?", (for_pro,))
    rows = cur.fetchall()
    conn.close()
    candidates = [r for r in rows if r[0] not in exclude_ids]
    if not candidates:
        return None
    return random.choice(candidates)

# Tournament DB helpers
def db_add_tournament_row(start_iso, topic, prizes, num_questions=5):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO tournaments (start_iso, topic, prizes, num_questions) VALUES (?, ?, ?, ?)",
                (start_iso, topic, prizes, num_questions))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid

def db_get_upcoming_tournaments():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, start_iso, topic, prizes, num_questions FROM tournaments ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def db_add_participant(tid, tg_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO tournament_participants (tournament_id, tg_id) VALUES (?, ?)", (tid, tg_id))
    cur.execute("INSERT OR IGNORE INTO tournament_scores (tournament_id, tg_id, correct_count) VALUES (?, ?, 0)", (tid, tg_id, 0))
    conn.commit()
    conn.close()

def db_get_participants(tid) -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT tg_id FROM tournament_participants WHERE tournament_id=?", (tid,))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows

def db_increment_score(tid, tg_id, inc=1):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE tournament_scores SET correct_count = correct_count + ? WHERE tournament_id=? AND tg_id=?", (inc, tid, tg_id))
    conn.commit()
    conn.close()

def db_get_scores(tid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT tg_id, correct_count FROM tournament_scores WHERE tournament_id=? ORDER BY correct_count DESC", (tid,))
    rows = cur.fetchall()
    conn.close()
    return rows

def db_get_random_tournament_questions(n=5):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, question, correct, opt1, opt2, opt3, opt4 FROM tournament_questions ORDER BY RANDOM() LIMIT ?", (n,))
    rows = cur.fetchall()
    conn.close()
    return rows

def db_add_tournament_question(q, correct, opts):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tournament_questions (question, correct, opt1, opt2, opt3, opt4) VALUES (?, ?, ?, ?, ?, ?)",
        (q, correct, opts[0], opts[1], opts[2], opts[3])
    )
    conn.commit()
    conn.close()

def db_list_tournament_questions():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, question, opt1, opt2, opt3, opt4, correct FROM tournament_questions")
    rows = cur.fetchall()
    conn.close()
    return rows

def db_delete_tournament_question(qid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM tournament_questions WHERE id=?", (qid,))
    conn.commit()
    conn.close()

# referral helpers
def add_referral(referrer_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET refer_count = refer_count + 1 WHERE tg_id=?", (referrer_id,))
    conn.commit()
    cur.execute("SELECT refer_count FROM users WHERE tg_id=?", (referrer_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return row[0]
    return 0

def set_pro_if_enough_refs(tg_id:int, threshold=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT refer_count, is_pro FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    refer_count, is_pro = row
    if is_pro:
        conn.close()
        return False
    if refer_count >= threshold:
        cur.execute("UPDATE users SET is_pro = 1 WHERE tg_id=?", (tg_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

# ---------------- Keyboards ----------------
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("📂 Profile"), KeyboardButton("🎮 Lager")],
        [KeyboardButton("🏆 Turnirlar"), KeyboardButton("👑 Pro")]
    ],
    resize_keyboard=True
)

# ---------------- FSM States for admin flows ----------------
class AdminCreateTournament(StatesGroup):
    waiting_for_datetime = State()
    waiting_for_topic = State()
    waiting_for_prizes = State()

class AdminAddTQ(StatesGroup):
    q_text = State()
    opt1 = State()
    opt2 = State()
    opt3 = State()
    opt4 = State()
    correct = State()

# ---------------- Utility functions ----------------
async def is_member_of_mandatory_channel(user_id: int) -> bool:
    if not MANDATORY_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(MANDATORY_CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        # bot may not be admin in channel or channel invalid; treat as not member
        logger.debug("channel check error: %s", e)
        return False

def local_now_iso():
    # naive local time iso (no tz) stored; could store tz-aware if desired
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

# ---------------- Handlers ----------------
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    args = message.get_args()  # referral param if present
    user = message.from_user
    # check mandatory channel
    joined = await is_member_of_mandatory_channel(user.id)
    if not joined:
        # send message with join button
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔔 Kanalga qo'shiling", url=f"https://t.me/{MANDATORY_CHANNEL.strip('@')}"),
            InlineKeyboardButton("✅ Tekshirish", callback_data="check_channel")
        )
        await message.answer("Botdan foydalanish uchun avvalo kanalimizga obuna bo'ling.", reply_markup=kb)
        return

    # add user if not exists
    db_add_user(user.id, user.full_name, "")

    # if referral param exists and valid, credit referrer
    if args:
        try:
            ref_id = int(args)
            if ref_id != user.id and db_get_user(ref_id):
                cnt = add_referral(ref_id)
                # if reach threshold, set pro
                became = set_pro_if_enough_refs(ref_id, threshold=10)
                try:
                    if became:
                        await bot.send_message(ref_id, f"🎉 Tabriklaymiz! Siz 10 ta referal to'pladingiz — sizga Pro obuna berildi!")
                    else:
                        await bot.send_message(ref_id, f"✅ Sizning refer_count: {cnt}")
                except:
                    pass
        except:
            pass

    # greet
    await message.answer(f"Salom, {user.full_name}!\nMenyu:", reply_markup=main_menu)

@dp.callback_query_handler(lambda c: c.data == "check_channel")
async def cb_check_channel(query: types.CallbackQuery):
    user = query.from_user
    ok = await is_member_of_mandatory_channel(user.id)
    if ok:
        await query.answer("Siz kanalga obuna bo'lgansiz. Davom eting.")
        await query.message.delete()
        await bot.send_message(user.id, "Menyu:", reply_markup=main_menu)
    else:
        await query.answer("Siz hali kanalga azolikni tasdiqlamadingiz.")

@dp.message_handler(content_types=types.ContentType.CONTACT)
async def contact_handler(message: types.Message):
    contact = message.contact
    if not contact or contact.user_id != message.from_user.id:
        await message.answer("Telefon raqamini o'zingiz yuboring.")
        return
    db_add_user(message.from_user.id, message.from_user.full_name, contact.phone_number)
    await message.answer("Ro'yxatdan o'tdingiz! Menyu:", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "📂 Profile")
async def profile_handler(message: types.Message):
    user = message.from_user
    row = db_get_user(user.id)
    if not row:
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("📲 Ro‘yxatdan o‘tish", callback_data="register"))
        await message.answer("Siz hali ro'yxatdan o'tmagansiz.", reply_markup=kb)
        return
    tg_id, name, phone, daraja, coin, is_pro, refer_count = row
    await message.answer(f"👤 Profil:\nIsm: {name}\nTelefon: {phone}\n⭐ Daraja: {daraja}\n💰 Coin: {coin}\nPro: {'Ha' if is_pro else 'Yo‘q'}\nReferallar: {refer_count}\nID: {tg_id}")

@dp.callback_query_handler(lambda c: c.data == "register")
async def cb_register(query: types.CallbackQuery):
    kb = ReplyKeyboardMarkup([[KeyboardButton("📲 Ro‘yxatdan o‘tish", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await bot.send_message(query.from_user.id, "Iltimos telefon raqamingizni yuboring:", reply_markup=kb)

@dp.message_handler(lambda m: m.text == "🎮 Lager")
async def lager_menu(message: types.Message):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🤖 AI bilan gaplashish", callback_data="ai_chat"))
    kb.add(InlineKeyboardButton("❓ Test savol olish", callback_data="quiz_start"))
    await message.answer("🎮 Lagerga xush kelibsiz!", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data in ("ai_chat", "quiz_start"))
async def cb_lager(query: types.CallbackQuery):
    await query.answer()
    if query.data == "ai_chat":
        await bot.send_message(query.from_user.id, "🤖 AI rejimi faollashtirildi. Endi yozing — men javob beraman.")
        # simple mode flag in memory (no persistence)
        await storage.set_data(user= query.from_user.id, data={"mode":"ai"})
    else:
        await send_quiz_for_user(query.from_user.id)

async def send_quiz_for_user(chat_id:int, for_pro=0):
    # fetch random question not seen
    seen = user_seen_questions.get(chat_id, set())
    q = db_get_random_question(for_pro=for_pro, exclude_ids=seen)
    if not q:
        await bot.send_message(chat_id, "Savollar qolmadi. Adminlarga murojaat qiling.")
        return
    qid, question, correct, opt1, opt2, opt3, opt4 = q
    options = [opt1, opt2, opt3, opt4]
    random.shuffle(options)
    # store last question in-memory per-user
    data = await storage.get_data(user=chat_id)
    data['last_q'] = {"qid": qid, "correct": correct}
    await storage.set_data(user=chat_id, data=data)
    kb = InlineKeyboardMarkup(row_width=1)
    for opt in options:
        kb.add(InlineKeyboardButton(opt, callback_data=f"ans|DB|{qid}|{opt}"))
    await bot.send_message(chat_id, f"❓ {question}", reply_markup=kb)
    s = user_seen_questions.get(chat_id, set())
    s.add(qid)
    user_seen_questions[chat_id] = s

@dp.callback_query_handler(lambda c: c.data.startswith("ans|"))
async def cb_answer(query: types.CallbackQuery):
    await query.answer()
    data = query.data
    user = query.from_user
    if data.startswith("ans|DB|"):
        parts = data.split("|",3)
        qid = int(parts[2]); chosen = parts[3]
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT correct FROM questions WHERE id=?", (qid,))
        row = cur.fetchone()
        conn.close()
        if not row:
            await query.message.edit_text("Savol topilmadi (bazada).")
            return
        correct = row[0]
        if chosen.strip().lower() == correct.strip().lower():
            db_update_score(user.id, daraja_inc=1, coin_inc=5)
            await query.message.edit_text("✅ To'g'ri! +1 ⭐ Daraja va +5 coin.")
        else:
            await query.message.edit_text(f"❌ Noto'g'ri. To'g'ri javob: {correct}")
    else:
        await query.message.edit_text("Xato callback.")

# ---------------- Turnir flows ----------------
@dp.message_handler(lambda m: m.text == "🏆 Turnirlar")
async def list_tournaments(message: types.Message):
    rows = db_get_upcoming_tournaments()
    if not rows:
        await message.answer("Hozircha turnirlar e'lon qilinmagan.")
        return
    text = "📢 Yaqin turnirlar:\n\n"
    kb = InlineKeyboardMarkup()
    for r in rows:
        tid, start_iso, topic, prizes, num_questions = r
        try:
            dt = datetime.fromisoformat(start_iso)
            dt_str = dt.strftime("%Y-%m-%d %H:%M")
        except:
            dt_str = start_iso
        text += f"ID:{tid} — {dt_str} — {topic}\nSovrinlar: {prizes}\n\n"
        kb.add(InlineKeyboardButton(f"Join Turnir {tid}", callback_data=f"join_t|{tid}"))
    # admin buttons
    if message.from_user.id == ADMIN_ID:
        kb.add(InlineKeyboardButton("➕ Yangi Turnir yaratish (admin)", callback_data="admin_addt"))
        kb.add(InlineKeyboardButton("📝 Turnir savol qo'shish (admin)", callback_data="admin_add_tq"))
        kb.add(InlineKeyboardButton("📋 Turnir savollarini ko'rish (admin)", callback_data="admin_list_tq"))
    await message.answer(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("join_t|"))
async def cb_join_t(query: types.CallbackQuery):
    await query.answer()
    parts = query.data.split("|")
    tid = int(parts[1])
    db_add_participant(tid, query.from_user.id)
    await query.message.edit_text("✅ Siz turnirga qoʻshildingiz. Turnir boshlanishida sizga xabar yuboriladi.")

# Admin create tournament flow (via callback)
@dp.callback_query_handler(lambda c: c.data == "admin_addt")
async def cb_admin_addt(query: types.CallbackQuery):
    uid = query.from_user.id
    if uid != ADMIN_ID:
        await query.answer("Siz admin emassiz!", show_alert=True)
        return
    await AdminCreateTournament.waiting_for_datetime.set()
    await bot.send_message(uid, "Turnir yaratish: Boshlash sanasini kiriting (YYYY-MM-DD HH:MM). Tashkent vaqtida.")

@dp.message_handler(state=AdminCreateTournament.waiting_for_datetime, content_types=types.ContentTypes.TEXT)
async def admin_addt_date(message: types.Message, state: FSMContext):
    text = message.text.strip()
    try:
        # parse simple format
        dt = datetime.fromisoformat(text)
        iso = dt.isoformat()
        await state.update_data(start_iso=iso)
        await AdminCreateTournament.next()
        await message.answer("Turnir mavzusini yozing (masalan: Matematika).")
    except Exception:
        await message.answer("Datetime format xato. Iltimos: YYYY-MM-DDTHH:MM[:ss] yoki YYYY-MM-DD HH:MM (ISO).")

@dp.message_handler(state=AdminCreateTournament.waiting_for_topic, content_types=types.ContentTypes.TEXT)
async def admin_addt_topic(message: types.Message, state: FSMContext):
    await state.update_data(topic=message.text.strip())
    await AdminCreateTournament.next()
    await message.answer("Sovrinlarni yozing (masalan: 1-orin: telefon; 2-orin: pul; 3-orin: Pro 1 oy).")

@dp.message_handler(state=AdminCreateTournament.waiting_for_prizes, content_types=types.ContentTypes.TEXT)
async def admin_addt_prizes(message: types.Message, state: FSMContext):
    data = await state.get_data()
    iso = data.get("start_iso")
    topic = data.get("topic")
    prizes = message.text.strip()
    tid = db_add_tournament_row(iso, topic, prizes, num_questions=5)
    # schedule tournament task
    task = asyncio.create_task(schedule_and_run_tournament(tid))
    tournament_tasks[tid] = task
    await message.answer(f"✅ Turnir e'lon qilindi. ID: {tid}. Boshlanish vaqti: {iso}")
    await state.finish()

# Admin add tournament question flow
@dp.callback_query_handler(lambda c: c.data == "admin_add_tq")
async def cb_admin_add_tq(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        await query.answer("Siz admin emassiz!", show_alert=True)
        return
    await AdminAddTQ.q_text.set()
    await bot.send_message(query.from_user.id, "📝 Turnir savolini yuboring (savol matni):")

@dp.message_handler(state=AdminAddTQ.q_text, content_types=types.ContentTypes.TEXT)
async def process_tq_q(message: types.Message, state: FSMContext):
    await state.update_data(q=message.text.strip())
    await AdminAddTQ.next()
    await message.answer("Variant 1 ni yuboring:")

@dp.message_handler(state=AdminAddTQ.opt1, content_types=types.ContentTypes.TEXT)
async def process_tq_opt1(message: types.Message, state: FSMContext):
    await state.update_data(opt1=message.text.strip())
    await AdminAddTQ.next()
    await message.answer("Variant 2 ni yuboring:")

@dp.message_handler(state=AdminAddTQ.opt2, content_types=types.ContentTypes.TEXT)
async def process_tq_opt2(message: types.Message, state: FSMContext):
    await state.update_data(opt2=message.text.strip())
    await AdminAddTQ.next()
    await message.answer("Variant 3 ni yuboring:")

@dp.message_handler(state=AdminAddTQ.opt3, content_types=types.ContentTypes.TEXT)
async def process_tq_opt3(message: types.Message, state: FSMContext):
    await state.update_data(opt3=message.text.strip())
    await AdminAddTQ.next()
    await message.answer("Variant 4 ni yuboring:")

@dp.message_handler(state=AdminAddTQ.opt4, content_types=types.ContentTypes.TEXT)
async def process_tq_opt4(message: types.Message, state: FSMContext):
    await state.update_data(opt4=message.text.strip())
    await AdminAddTQ.next()
    await message.answer("To'g'ri javobni yozing (butun matn bilan):")

@dp.message_handler(state=AdminAddTQ.correct, content_types=types.ContentTypes.TEXT)
async def process_tq_correct(message: types.Message, state: FSMContext):
    data = await state.get_data()
    q = data.get("q")
    opts = [data.get("opt1"), data.get("opt2"), data.get("opt3"), data.get("opt4")]
    correct = message.text.strip()
    if not (q and len(opts) == 4):
        await message.answer("Savol yoki variantlar yetarli emas. Operatsiya bekor qilindi.")
        await state.finish()
        return
    db_add_tournament_question(q, correct, opts)
    await message.answer("✅ Turnir savoli saqlandi.")
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "admin_list_tq")
async def cb_admin_list_tq(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        await query.answer("Siz admin emassiz!", show_alert=True)
        return
    rows = db_list_tournament_questions()
    if not rows:
        await query.message.edit_text("Turnir savollari mavjud emas.")
        return
    text = "Turnir savollari:\n\n"
    for r in rows:
        qid, question, opt1, opt2, opt3, opt4, correct = r
        text += f"ID:{qid}\n{question}\n1) {opt1}\n2) {opt2}\n3) {opt3}\n4) {opt4}\n\n"
    await query.message.edit_text(text)

# Tournament scheduling and runtime
async def schedule_and_run_tournament(tid:int):
    # fetch tournament row
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT start_iso, topic, prizes, num_questions FROM tournaments WHERE id=?", (tid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        logger.error("Turnir topilmadi: %s", tid)
        return
    start_iso, topic, prizes, num_q = row
    try:
        start_dt = datetime.fromisoformat(start_iso)
    except:
        logger.error("Turnir datetime parse xato for tid=%s", tid)
        return
    # compute wait seconds relative to UTC naive storage
    now = datetime.utcnow()
    wait = (start_dt - now).total_seconds()
    if wait > 0:
        logger.info("Scheduling tournament %s to run in %s seconds", tid, wait)
        await asyncio.sleep(wait)
    # start tournament
    participants = db_get_participants(tid)
    if not participants:
        logger.info("No participants for tournament %s", tid)
        return
    # notify
    for tg in participants:
        try:
            await bot.send_message(tg, f"🏁 Turnir '{topic}' boshlandi! Har savol uchun 20 soniya.")
        except:
            pass
    # get questions
    qrows = db_get_random_tournament_questions(n=num_q)
    if not qrows:
        logger.info("No tournament questions for %s", tid)
        return
    for q in qrows:
        qid, question, correct, opt1, opt2, opt3, opt4 = q
        options = [opt1, opt2, opt3, opt4]
        random.shuffle(options)
        GLOBAL_TOURNAMENT_ANSWERS[(tid, qid)] = {}
        kb = InlineKeyboardMarkup(row_width=1)
        for opt in options:
            kb.add(InlineKeyboardButton(opt, callback_data=f"tans|{tid}|{qid}|{opt}"))
        for tg in participants:
            try:
                await bot.send_message(tg, f"❓ Turnir savoli:\n{question}\nJavobni tanlang. (20 s)", reply_markup=kb)
            except:
                pass
        # wait 20 seconds
        await asyncio.sleep(20)
        collected = GLOBAL_TOURNAMENT_ANSWERS.pop((tid, qid), {})
        # evaluate
        for tg in participants:
            chosen = collected.get(tg)
            if not chosen:
                try:
                    await bot.send_message(tg, "⏱️ Vaqt tugadi. Siz javob bermadingiz.")
                except:
                    pass
            else:
                if chosen.strip().lower() == correct.strip().lower():
                    db_increment_score(tid, tg, inc=1)
                    try:
                        await bot.send_message(tg, "✅ To'g'ri!")
                    except:
                        pass
                else:
                    try:
                        await bot.send_message(tg, f"❌ Noto'g'ri. To'g'ri javob: {correct}")
                    except:
                        pass
        await asyncio.sleep(1)
    # finalize results
    scores = db_get_scores(tid)
    top3 = scores[:3]
    final_text = f"🏆 Turnir '{topic}' yakunlandi!\nSovrinlar: {prizes}\n\nTop natijalar:\n"
    rank = 1
    for r in top3:
        tg_id, cnt = r
        final_text += f"{rank}. ID:{tg_id} — {cnt} to'g'ri javob\n"
        rank += 1
    for tg in participants:
        try:
            await bot.send_message(tg, final_text)
        except:
            pass
    try:
        await bot.send_message(ADMIN_ID, final_text)
    except:
        pass

@dp.callback_query_handler(lambda c: c.data.startswith("tans|"))
async def cb_tournament_answer(query: types.CallbackQuery):
    await query.answer()
    parts = query.data.split("|",3)
    if len(parts) != 4:
        await query.message.edit_text("Xato callback")
        return
    _, tid_s, qid_s, opt = parts
    tid = int(tid_s); qid = int(qid_s)
    key = (tid, qid)
    tg = query.from_user.id
    if key not in GLOBAL_TOURNAMENT_ANSWERS:
        await query.message.edit_text("⏳ Javob qabul qilinmaydi yoki siz ortda qoldingiz.")
        return
    GLOBAL_TOURNAMENT_ANSWERS[key][tg] = opt
    try:
        await query.message.edit_text("✅ Javob qabul qilindi. Natija 20s tugagach e'lon qilinadi.")
    except:
        pass

# ---------------- Generic handlers ----------------
@dp.message_handler(lambda m: m.text == "👑 Pro")
async def pro_info(message: types.Message):
    row = db_get_user(message.from_user.id)
    is_pro = row[5] if row else 0
    await message.answer(f"👑 PRO haqida:\nPro: {'Ha' if is_pro else 'Yo‘q'}\n10 referal yig‘sang Pro tekin bo‘ladi.\n(To‘lov funksiyasi keyinchalik qo‘shiladi.)")

@dp.message_handler()
async def catch_all(message: types.Message):
    # simple AI mode check
    data = await storage.get_data(user=message.from_user.id)
    if data.get("mode") == "ai":
        # placeholder reply
        await message.answer("🤖 (AI) Hozir AI integratsiyasi yoqilgan emas.")
        return
    await message.answer("Menyudan tanlang yoki /start bosing.", reply_markup=main_menu)

# ---------------- Startup scheduling ----------------
async def on_startup(dp):
    # create DB
    init_db()
    # schedule existing tournaments in future
    rows = db_get_upcoming_tournaments()
    now = datetime.utcnow()
    for r in rows:
        tid, start_iso, topic, prizes, num_questions = r
        try:
            start_dt = datetime.fromisoformat(start_iso)
            if start_dt > now:
                task = asyncio.create_task(schedule_and_run_tournament(tid))
                tournament_tasks[tid] = task
                logger.info("Scheduled tournament %s at %s", tid, start_dt)
        except Exception as e:
            logger.exception("Error scheduling tournament %s: %s", tid, e)
    logger.info("Bot started.")

if __name__ == "__main__":
    # basic safety checks
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        print("Set BOT_TOKEN in the script before running.")
        exit(1)
    executor.start_polling(dp, on_startup=on_startup)
