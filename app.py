import os
import json
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import requests
import gspread
from flask import Flask, request, abort
from google.oauth2.service_account import Credentials

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
ALLOWED_IDS = {int(x) for x in os.environ["ALLOWED_USER_IDS"].split(",")}
SHEET_ID = os.environ["SHEET_ID"]

TZ = ZoneInfo("Europe/Istanbul")
CATS = "Kategoriler"
RECS = "Kayitlar"
MAX_CAT_LEN = 30
RESTORE_DAYS = 30
MAX_MSG = 4000
HELP = (
    "Komutlar:\n"
    "/add-categories music : kategori oluştur\n"
    "/add music sokak köşelerinde : kayıt ekle\n"
    "/music : kategori listesini göster\n"
    "/categories : tüm kategoriler\n"
    "/delete-category music : silme önizlemesi\n"
    "/delete-category music confirm : arşivle\n"
    "/restore-category music : arşivden geri al"
)

app = Flask(__name__)
_seen_updates = set()


def now():
    return datetime.now(TZ)


def norm(s):
    """Küçük harf + Türkçe karakter sadeleştirme. 'Müzik' ve 'muzik' aynı sayılır."""
    s = s.strip().replace("İ", "i").replace("I", "i").lower()
    return s.translate(str.maketrans("ığşüöç", "igsuoc"))


@lru_cache(maxsize=1)
def book():
    info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def rows(tab):
    return book().worksheet(tab).get_all_records()


def find_cat(key):
    """Satır numarasını (1 tabanlı, başlık dahil) ve kayıt sözlüğünü döndürür."""
    for i, r in enumerate(rows(CATS), start=2):
        if r["anahtar"] == key:
            return i, r
    return None, None


def send(chat_id, text):
    for start in range(0, len(text), MAX_MSG):
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text[start:start + MAX_MSG]},
            timeout=10,
        )


# ---------- Kategori işlemleri ----------

def add_category(name):
    name = name.strip()
    if len(name) > MAX_CAT_LEN:
        return f"Kategori adı en fazla {MAX_CAT_LEN} karakter olabilir."
    key = norm(name)
    _, cat = find_cat(key)
    if cat and cat["durum"] == "aktif":
        return f"'{name}' zaten var."
    if cat:
        return f"'{name}' arşivde. Geri almak için /restore-category {name}"
    book().worksheet(CATS).append_row(
        [name, key, "aktif", now().isoformat(timespec="seconds"), ""]
    )
    return f"✅ '{name}' kategorisi oluşturuldu."


def list_categories():
    active = [r["ad"] for r in rows(CATS) if r["durum"] == "aktif"]
    if not active:
        return "Henüz kategori yok. /add-categories music ile başlayın."
    return "📂 Kategoriler:\n" + "\n".join(f"• {a}" for a in active)


def delete_category(name, confirm):
    key = norm(name)
    row, cat = find_cat(key)
    if not cat or cat["durum"] != "aktif":
        return "Aktif böyle bir kategori yok."
    count = sum(1 for r in rows(RECS) if r["kategori"] == key)
    if not confirm:
        return (
            f"⚠️ '{cat['ad']}' arşivlenecek ({count} kayıt gizlenecek).\n"
            f"Onaylamak için: /delete-category {name} confirm\n"
            f"{RESTORE_DAYS} gün içinde geri alınabilir."
        )
    ws = book().worksheet(CATS)
    ws.update_cell(row, 3, "arsiv")
    ws.update_cell(row, 5, now().isoformat(timespec="seconds"))
    return f"🗄️ '{cat['ad']}' arşivlendi. Geri almak: /restore-category {name}"


def restore_category(name):
    key = norm(name)
    row, cat = find_cat(key)
    if not cat:
        return "Böyle bir kategori yok."
    if cat["durum"] == "aktif":
        return "Kategori zaten aktif."
    archived_at = datetime.fromisoformat(str(cat["arsiv_tarihi"]))
    if now() - archived_at > timedelta(days=RESTORE_DAYS):
        return f"Geri alma süresi ({RESTORE_DAYS} gün) dolmuş."
    ws = book().worksheet(CATS)
    ws.update_cell(row, 3, "aktif")
    ws.update_cell(row, 5, "")
    return f"♻️ '{cat['ad']}' geri getirildi."


# ---------- Kayıt işlemleri ----------

def add_record(cat_name, content, user):
    key = norm(cat_name)
    _, cat = find_cat(key)
    if not cat or cat["durum"] != "aktif":
        return f"'{cat_name}' kategorisi yok. Önce /add-categories {cat_name} yazın."
    content = content.strip()
    ckey = norm(content)
    if any(r["kategori"] == key and r["icerik_anahtar"] == ckey for r in rows(RECS)):
        return "Bu kayıt zaten var, eklenmedi."
    book().worksheet(RECS).append_row([
        uuid.uuid4().hex[:8], key, content, ckey,
        user.get("first_name", "?"), "bekliyor",
        now().isoformat(timespec="seconds"),
    ])
    return f"✅ {cat['ad']} listesine eklendi: {content}"


def list_records(cmd):
    """/music, /muzik, /film gibi komutlar; kategori adından dinamik eşleşir."""
    key = norm(cmd)
    _, cat = find_cat(key)
    if not cat or cat["durum"] != "aktif":
        return "Bilinmeyen komut veya kategori. /yardim yazın."
    items = [r for r in rows(RECS) if r["kategori"] == key]
    if not items:
        return f"{cat['ad']} listesi boş."
    lines = [
        f"{i}. {r['icerik']}  [{r['durum']}, {r['ekleyen']}]"
        for i, r in enumerate(items, 1)
    ]
    return f"📌 {cat['ad']}\n" + "\n".join(lines)


# ---------- Komut yönlendirme ----------

def handle(user, text):
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0][1:].split("@")[0].lower()
    args = parts[1:]

    if cmd in ("add-categories", "add-catagories", "add_categories"):
        if len(args) != 1:
            return "Kullanım: /add-categories music (tek kelime)"
        return add_category(args[0])
    if cmd == "add":
        if len(args) < 2:
            return "Kullanım: /add music sokak köşelerinde"
        return add_record(args[0], text.split(maxsplit=2)[2], user)
    if cmd == "categories":
        return list_categories()
    if cmd == "delete-category":
        if not args:
            return "Kullanım: /delete-category music"
        confirm = len(args) > 1 and args[1].lower() == "confirm"
        return delete_category(args[0], confirm)
    if cmd == "restore-category":
        if not args:
            return "Kullanım: /restore-category music"
        return restore_category(args[0])
    if cmd in ("yardim", "help", "start"):
        return HELP
    return list_records(cmd)


@app.get("/")
def health():
    return "ok"


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(404)

    update = request.get_json(silent=True) or {}
    update_id = update.get("update_id")
    if update_id in _seen_updates:  # Telegram yeniden denemesi, tekrar işleme
        return "ok"
    _seen_updates.add(update_id)

    msg = update.get("message")
    if not msg or "text" not in msg:
        return "ok"

    user = msg.get("from", {})
    if user.get("id") not in ALLOWED_IDS:  # yetkisiz: sessizce çık
        return "ok"

    reply = handle(user, msg["text"])
    if reply:
        send(msg["chat"]["id"], reply)
    return "ok"
