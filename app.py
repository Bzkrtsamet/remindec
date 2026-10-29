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
ALLOWED_IDS = {int(x) for x in os.environ["ALLOWED_USER_IDS"].split(",") if x.strip()}
SHEET_ID = os.environ["SHEET_ID"]

TZ = ZoneInfo("Europe/Istanbul")
CATS = "Kategoriler"
RECS = "Kayitlar"
MAX_CAT_LEN = 30
RESTORE_DAYS = 30
MAX_MSG = 4000
ACTIONS = {"addc", "delc", "restorec", "add", "bitti", "geri", "del"}

HELP = (
    "📖 Komutlar\n\n"
    "Kategori:\n"
    "/addc music : kategori ekle\n"
    "/delc music : silme önizlemesi\n"
    "/delc music onay : kategoriyi arşivle\n"
    "/restorec music : arşivden geri al\n"
    "/categories : tüm kategoriler\n\n"
    "Kayıt:\n"
    "/add music sokagın ruyası : kayıt ekle\n"
    "/bitti music sokagın ruyası : tamamlandı işaretle\n"
    "/geri music sokagın ruyası : tamamlanmayı geri al\n"
    "/del music sokagın ruyası : kaydı sil\n\n"
    "Listeleme:\n"
    "/music : müzik listesini göster\n"
    "(her kategori adı kendi listesini açar)\n\n"
    "Ortak sohbette: @botadi music yazın"
)

COMMANDS = [
    {"command": "addc", "description": "Kategori ekle: /addc music"},
    {"command": "delc", "description": "Kategori sil (önizleme / onay)"},
    {"command": "restorec", "description": "Arşivlenen kategoriyi geri al"},
    {"command": "categories", "description": "Tüm kategorileri listele"},
    {"command": "add", "description": "Kayıt ekle: /add music isim"},
    {"command": "bitti", "description": "Kaydı tamamlandı işaretle"},
    {"command": "geri", "description": "Tamamlanmayı geri al"},
    {"command": "del", "description": "Kaydı sil"},
    {"command": "yardim", "description": "Komut listesi"},
]

app = Flask(__name__)
_seen_updates = set()


# ---------- Yardımcılar ----------

def now():
    return datetime.now(TZ)


def iso():
    return now().isoformat(timespec="seconds")


def norm(s):
    """Küçük harf + Türkçe karakter sadeleştirme. 'Müzik' ve 'muzik' aynı sayılır."""
    s = str(s).strip().replace("İ", "i").replace("I", "i").lower()
    return s.translate(str.maketrans("ığşüöç", "igsuoc"))


@lru_cache(maxsize=1)
def book():
    info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def get_ws(tab):
    """Sekmeyi büyük/küçük harf ve Türkçe karakterden bağımsız bulur."""
    sh = book()
    titles = [w.title for w in sh.worksheets()]
    for t in titles:
        if norm(t) == norm(tab):
            return sh.worksheet(t)
    raise RuntimeError(f"Sekme bulunamadı: '{tab}'. Mevcut sekmeler: {titles}")


def rows(tab):
    return get_ws(tab).get_all_records()


def tg(method, payload):
    return requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        json=payload,
        timeout=10,
    )


def send(chat_id, text):
    for start in range(0, len(text), MAX_MSG):
        tg("sendMessage", {"chat_id": chat_id, "text": text[start:start + MAX_MSG]})


def set_commands():
    try:
        tg("setMyCommands", {"commands": COMMANDS})
    except Exception:
        app.logger.exception("setMyCommands başarısız")


set_commands()


def find_cat(key):
    """(satır_no, kategori_sözlüğü) döndürür. Satır 1 tabanlı, başlık dahil."""
    for i, r in enumerate(rows(CATS), start=2):
        if str(r["anahtar"]) == key:
            return i, r
    return None, None


# ---------- Kategori işlemleri ----------

def add_category(name):
    name = name.strip()
    if not name:
        return "Kategori adı boş olamaz."
    if len(name) > MAX_CAT_LEN:
        return f"Kategori adı en fazla {MAX_CAT_LEN} karakter olabilir."
    key = norm(name)
    _, cat = find_cat(key)
    if cat and cat["durum"] == "aktif":
        return f"'{cat['ad']}' zaten var."
    if cat:
        return f"'{cat['ad']}' arşivde. Geri almak için /restorec {name}"
    get_ws(CATS).append_row([name, key, "aktif", iso(), ""], value_input_option="RAW")
    return f"✅ '{name}' kategorisi oluşturuldu."


def list_categories():
    active = [str(r["ad"]) for r in rows(CATS) if r["durum"] == "aktif"]
    if not active:
        return "Henüz kategori yok. /addc music ile başlayın."
    return "📂 Kategoriler:\n" + "\n".join(f"• {a}" for a in active)


def delete_category(name, confirm):
    key = norm(name)
    row, cat = find_cat(key)
    if not cat or cat["durum"] != "aktif":
        return "Aktif böyle bir kategori yok."
    count = sum(
        1 for r in rows(RECS)
        if r["kategori"] == key and r["durum"] != "silindi"
    )
    if not confirm:
        return (
            f"⚠️ '{cat['ad']}' arşivlenecek ({count} kayıt gizlenecek).\n"
            f"Onaylamak için: /delc {name} onay\n"
            f"{RESTORE_DAYS} gün içinde /restorec ile geri alınabilir."
        )
    ws = get_ws(CATS)
    ws.update_cell(row, 3, "arsiv")
    ws.update(f"E{row}", [[iso()]], value_input_option="RAW")
    return f"🗄️ '{cat['ad']}' arşivlendi. Geri almak: /restorec {name}"


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
    ws = get_ws(CATS)
    ws.update_cell(row, 3, "aktif")
    ws.update(f"E{row}", [[""]], value_input_option="RAW")
    return f"♻️ '{cat['ad']}' geri getirildi."


# ---------- Kayıt işlemleri ----------

def add_record(cat_name, content, user):
    key = norm(cat_name)
    _, cat = find_cat(key)
    if not cat or cat["durum"] != "aktif":
        return f"'{cat_name}' kategorisi yok. Önce /addc {cat_name} yazın."
    content = content.strip()
    if not content:
        return f"Kullanım: /add {cat_name} sokagın ruyası"
    ckey = norm(content)

    ws = get_ws(RECS)
    for i, r in enumerate(rows(RECS), start=2):
        if r["kategori"] == key and r["icerik_anahtar"] == ckey:
            if r["durum"] == "silindi":
                ws.update_cell(i, 6, "bekliyor")
                return f"♻️ Daha önce silinmişti, geri eklendi: {r['icerik']}"
            return "Bu kayıt zaten var, eklenmedi."

    ws.append_row(
        [uuid.uuid4().hex[:8], key, content, ckey,
         user.get("first_name", "?"), "bekliyor", iso()],
        value_input_option="RAW",
    )
    return f"✅ {cat['ad']} listesine eklendi: {content}"


def find_record(cat_key, text):
    """Dönüş: ((satır_no, kayıt), None) veya (None, hata_mesajı)."""
    ckey = norm(text)
    hits = [
        (i, r) for i, r in enumerate(rows(RECS), start=2)
        if r["kategori"] == cat_key and r["durum"] != "silindi"
    ]
    exact = [h for h in hits if h[1]["icerik_anahtar"] == ckey]
    if len(exact) == 1:
        return exact[0], None
    cands = exact or [h for h in hits if ckey and ckey in h[1]["icerik_anahtar"]]
    if not cands:
        return None, "Eşleşen kayıt bulunamadı."
    if len(cands) > 1:
        names = "\n".join(f"• {r['icerik']}" for _, r in cands[:10])
        return None, f"Birden fazla eşleşme var, daha uzun yazın:\n{names}"
    return cands[0], None


def set_record_status(cat_name, text, status, label):
    key = norm(cat_name)
    _, cat = find_cat(key)
    if not cat or cat["durum"] != "aktif":
        return f"'{cat_name}' kategorisi yok."
    hit, err = find_record(key, text)
    if err:
        return err
    row, rec = hit
    get_ws(RECS).update_cell(row, 6, status)
    return f"{label}: {rec['icerik']}"


def list_records(cmd):
    key = norm(cmd)
    _, cat = find_cat(key)
    if not cat or cat["durum"] != "aktif":
        return "Bilinmeyen komut veya kategori. /yardim yazın."
    items = [
        r for r in rows(RECS)
        if r["kategori"] == key and r["durum"] != "silindi"
    ]
    if not items:
        return f"{cat['ad']} listesi boş. /add {cat['ad']} ... ile ekleyin."
    lines = []
    for i, r in enumerate(items, 1):
        mark = "✅ " if r["durum"] == "tamamlandi" else ""
        lines.append(f"{i}. {mark}{r['icerik']}  [{r['ekleyen']}]")
    return f"📌 {cat['ad']}\n" + "\n".join(lines)


# ---------- Komut yönlendirme (normal mesajlar) ----------

def handle(user, text):
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd = parts[0][1:].split("@")[0].lower()
    args = parts[1:]

    # Kategori
    if cmd == "addc":
        if len(args) != 1:
            return "Kullanım: /addc music"
        return add_category(args[0])
    if cmd == "delc":
        if not args:
            return "Kullanım: /delc music"
        confirm = len(args) > 1 and norm(args[1]) in ("onay", "confirm")
        return delete_category(args[0], confirm)
    if cmd == "restorec":
        return restore_category(args[0]) if args else "Kullanım: /restorec music"
    if cmd == "categories":
        return list_categories()

    # Kayıt
    if cmd == "add":
        if len(args) < 2:
            return "Kullanım: /add music sokagın ruyası"
        return add_record(args[0], text.split(maxsplit=2)[2], user)
    if cmd in ("del", "bitti", "geri"):
        if len(args) < 2:
            return f"Kullanım: /{cmd} music sokagın ruyası"
        content = text.split(maxsplit=2)[2]
        if cmd == "del":
            return set_record_status(args[0], content, "silindi", "🗑️ Silindi")
        if cmd == "bitti":
            return set_record_status(args[0], content, "tamamlandi", "✅ Tamamlandı")
        return set_record_status(args[0], content, "bekliyor", "↩️ Geri alındı")

    if cmd in ("yardim", "help", "start"):
        return HELP

    # Kategori adıyla listeleme: /music, /muzik, /film ...
    return list_records(cmd)


# ---------- Inline mod (ortak sohbet) ----------

def inline_results(query):
    """@bot yazılan metne göre sonuç listesi üretir. Yazma yapmaz."""
    q = query.strip()
    if not q:
        return []
    parts = q.split(maxsplit=1)
    first = norm(parts[0])

    # İşlem sonuçları: dokununca chosen_inline_result ile çalışır
    if first in ACTIONS:
        needed = 2 if first in ("addc", "delc", "restorec") else 3
        if len(q.split()) < needed:
            return []
        return [{
            "type": "article",
            "id": "act",
            "title": f"▶ {q}",
            "description": "Dokununca çalışır ve sohbete yazılır",
            "input_message_content": {"message_text": f"/{q}"},
        }]

    # Listeleme: "music" veya "music sokak"
    _, cat = find_cat(first)
    if not cat or cat["durum"] != "aktif":
        return []
    filt = norm(parts[1]) if len(parts) > 1 else ""
    items = [
        r for r in rows(RECS)
        if r["kategori"] == first and r["durum"] != "silindi"
        and (not filt or filt in r["icerik_anahtar"])
    ]
    results = []
    for i, r in enumerate(items[:50]):
        mark = "✅ " if r["durum"] == "tamamlandi" else ""
        results.append({
            "type": "article",
            "id": f"l{i}",
            "title": f"{mark}{r['icerik']}",
            "description": f"{cat['ad']} · {r['ekleyen']}",
            "input_message_content": {"message_text": r["icerik"]},
        })
    return results


def handle_inline(iq):
    user = iq.get("from", {})
    if user.get("id") not in ALLOWED_IDS:
        return  # yetkisiz: hiç yanıt yok
    try:
        results = inline_results(iq.get("query", ""))
    except Exception:
        app.logger.exception("Inline hatası")
        results = []
    tg("answerInlineQuery", {
        "inline_query_id": iq["id"],
        "results": results,
        "cache_time": 0,
        "is_personal": True,
    })


def handle_chosen(ch):
    user = ch.get("from", {})
    if user.get("id") not in ALLOWED_IDS:
        return
    if not str(ch.get("result_id", "")).startswith("act"):
        return  # listeleme sonuçlarında yazma yok
    text = "/" + ch.get("query", "").strip()
    try:
        reply = handle(user, text)
    except Exception as e:
        app.logger.exception("Chosen hatası")
        reply = f"⚠️ Hata: {type(e).__name__}: {str(e)[:300]}"
    # Sonucu botun size özel sohbetinize gönderiyoruz
    if reply:
        send(user["id"], reply)


# ---------- Webhook ----------

@app.get("/")
def health():
    return "ok"


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(404)

    update = request.get_json(silent=True) or {}
    update_id = update.get("update_id")
    if update_id in _seen_updates:
        return "ok"
    _seen_updates.add(update_id)

    if "inline_query" in update:
        handle_inline(update["inline_query"])
        return "ok"
    if "chosen_inline_result" in update:
        handle_chosen(update["chosen_inline_result"])
        return "ok"

    msg = update.get("message")
    if not msg or "text" not in msg:
        return "ok"

    user = msg.get("from", {})
    if user.get("id") not in ALLOWED_IDS:
        return "ok"  # yetkisiz kullanıcı: hiç yanıt yok

    try:
        reply = handle(user, msg["text"])
    except Exception as e:
        app.logger.exception("Komut hatası")
        reply = f"⚠️ Hata: {type(e).__name__}: {str(e)[:300]}"

    if reply:
        send(msg["chat"]["id"], reply)
    return "ok"
