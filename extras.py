# -*- coding: utf-8 -*-
"""
extras.py — MusicBot V4 : accès, invités, panel admin, avis, fin de session.

Branché sur le bot telebot de main.py via extras.install(...).
Rien de secret n'est écrit en clair ici : le code d'accès et la phrase secrète
du propriétaire sont stockés sous forme d'empreintes PBKDF2 (voir make_hash.py).
"""

import hashlib
import hmac
import html
import logging
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import unicodedata

from telebot import types

log = logging.getLogger("musicbot.extras")

# ============================================================
# CONFIGURATION
# ============================================================

DB_FILE = os.environ.get("DB_FILE", "musicbot.db")
OWNER_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("OWNER_IDS", ""))}
OWNER_NAME = os.environ.get("OWNER_NAME", "Abdul Yeo")
BOT_NAME = os.environ.get("BOT_NAME", "Téo")
CONTACT_TG = os.environ.get("CONTACT_TELEGRAM", "https://t.me/Abdulyeo")
CONTACT_WA = os.environ.get("CONTACT_WHATSAPP", "https://wa.me/2250500778962")


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


SESSION_IDLE = _env_int("SESSION_IDLE", 60)       # secondes sans activité avant « ça sera tout »
SESSION_MIN_DL = _env_int("SESSION_MIN_DL", 2)    # téléchargements minimum pour déclencher le message
GUEST_DAYS = _env_int("GUEST_DAYS", 7)
MAX_ATTEMPTS = 3
LOCK_SECONDS = 15 * 60

# Empreintes (PBKDF2). Pour changer : python make_hash.py, puis variables d'environnement.
ACCESS_CODE_HASH = os.environ.get(
    "ACCESS_CODE_HASH",
    "pbkdf2$200000$1dad4c87d6f0b64b$0bebb2d572a38ec57276b0900ab57ff8eb63a13c1243f51bfa3c912cae467ad1",
)
OWNER_PHRASE_HASH = os.environ.get(
    "OWNER_PHRASE_HASH",
    "pbkdf2$200000$dd5fa78f9d8609f9$9b23a29dbdea1427bc99b35315a1eeaac8e093795411641147684ecc855a4ac5",
)

# ============================================================
# OUTILS SECRETS
# ============================================================

_ITER = 200_000


def hash_secret(value, salt=None):
    salt = salt or secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), _ITER)
    return "pbkdf2$%d$%s$%s" % (_ITER, salt, dk.hex())


def verify_secret(value, stored):
    try:
        _, iters, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", value.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


def _strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))


def norm_code(text):
    """Code : lettres/chiffres uniquement, majuscules (espaces et tirets ignorés)."""
    return re.sub(r"[^A-Z0-9]", "", _strip_accents(text).upper())


def norm_phrase(text):
    """Phrase secrète : minuscules, sans accents, sans ponctuation ni espaces."""
    return re.sub(r"[^a-z0-9]", "", _strip_accents(text).lower())


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ============================================================
# BASE DE DONNÉES
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    uid INTEGER PRIMARY KEY, first_name TEXT, username TEXT,
    role TEXT NOT NULL DEFAULT 'none', access_until REAL,
    banned INTEGER NOT NULL DEFAULT 0, created REAL, last_seen REAL,
    dl_count INTEGER NOT NULL DEFAULT 0, notif_off INTEGER NOT NULL DEFAULT 0,
    reminded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS codes(
    hash TEXT PRIMARY KEY, bound_uid INTEGER, created REAL, expires REAL,
    used_by INTEGER, used_at REAL);
CREATE TABLE IF NOT EXISTS requests(
    uid INTEGER PRIMARY KEY, name TEXT, username TEXT, status TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT,
    plus TEXT, moins TEXT, note INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS downloads(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, artist TEXT, title TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS idx_dl_uid ON downloads(uid, ts);
"""

_db_lock = threading.RLock()
_conn = None


def _db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
        try:
            _conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def q(sql, args=(), fetch=None):
    with _db_lock:
        cur = _db().execute(sql, args)
        out = None
        if fetch == "one":
            out = cur.fetchone()
        elif fetch == "all":
            out = cur.fetchall()
        _db().commit()
        return out


def now():
    return time.time()


def fmt_date(ts):
    return time.strftime("%d/%m %H:%M", time.gmtime(ts)) if ts else "—"


# ============================================================
# ÉTAT EN MÉMOIRE
# ============================================================

_gate_state = {}      # uid -> {"claim": bool}
_attempts = {}        # uid -> essais ratés
_lock_until = {}      # uid -> timestamp
_guest_view = set()   # propriétaires qui testent la vue invité
_fb = {}              # uid -> brouillon d'avis
_bc_wait = set()      # propriétaires en train d'écrire une annonce
_bc_draft = {}        # uid -> texte
_sess = {}            # uid -> session d'écoute
_sess_lock = threading.Lock()
_errors = {"count": 0}

# Injectés par install()
bot = None
_send_fn = None
_menu_fn = None
_busy_fn = None
_esc = lambda v: html.escape(str(v or ""), quote=False)  # noqa: E731


def _send(chat_id, text, **kw):
    kw.setdefault("parse_mode", "HTML")
    return bot.send_message(chat_id, text, **kw)


def _safe_send(chat_id, text, **kw):
    try:
        return _send(chat_id, text, **kw)
    except Exception:
        log.warning("Envoi impossible à %s", chat_id, exc_info=True)
        return None


def _del(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass


def _answer(call, text="", alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=alert)
    except Exception:
        pass


# ============================================================
# STATUT / RÔLES
# ============================================================

def _owner_identity(uid):
    if uid in OWNER_IDS:
        return True
    row = q("SELECT role FROM users WHERE uid=?", (uid,), "one")
    return bool(row and row[0] == "owner")


def owners():
    ids = set(OWNER_IDS)
    for (uid,) in q("SELECT uid FROM users WHERE role='owner'", fetch="all"):
        ids.add(uid)
    return ids


def status_of(uid):
    """owner | ok | expired | banned | none"""
    if uid in _guest_view:
        return "none"
    if uid in OWNER_IDS:
        return "owner"
    row = q("SELECT role, access_until, banned FROM users WHERE uid=?", (uid,), "one")
    if not row:
        return "none"
    role, until, banned = row
    if banned:
        return "banned"
    if role == "owner":
        return "owner"
    if role == "member":
        return "ok"
    if role == "guest":
        return "ok" if until and until > now() else "expired"
    return "none"


def is_ok(uid):
    return status_of(uid) in ("owner", "ok")


def is_owner(uid):
    return status_of(uid) == "owner"


def _grant(user, role, until=None):
    row = q("SELECT role FROM users WHERE uid=?", (user.id,), "one")
    if row and row[0] == "owner" and role != "owner":
        return
    t = now()
    q("""INSERT INTO users(uid, first_name, username, role, access_until, banned, created, last_seen, reminded)
         VALUES(?,?,?,?,?,0,?,?,0)
         ON CONFLICT(uid) DO UPDATE SET first_name=excluded.first_name, username=excluded.username,
            role=excluded.role, access_until=excluded.access_until, banned=0, reminded=0""",
      (user.id, user.first_name or "", user.username or "", role, until, t, t))


def _first_name(uid, default="ami"):
    row = q("SELECT first_name FROM users WHERE uid=?", (uid,), "one")
    return (row[0] if row and row[0] else None) or default


def _tell_owners(text, kb=None):
    for oid in owners():
        _safe_send(oid, text, reply_markup=kb)


# ============================================================
# CODES INVITÉS
# ============================================================

def new_guest_code(bound_uid=None):
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    code = "TEO-%s-%s" % (raw[:5], raw[5:])
    q("INSERT INTO codes(hash, bound_uid, created, expires) VALUES(?,?,?,?)",
      (_sha(norm_code(code)), bound_uid, now(), now() + GUEST_DAYS * 86400))
    return code


def _try_guest_code(uid, n):
    """Retourne 'ok' | 'expired' | 'other' | None (code inconnu)."""
    row = q("SELECT bound_uid, expires, used_by FROM codes WHERE hash=?", (_sha(n),), "one")
    if not row:
        return None
    bound, expires, used_by = row
    if bound and bound != uid:
        return "other"
    if used_by and used_by != uid:
        return "other"
    if used_by == uid:
        # Déjà activé par cette personne : pas de rallonge possible.
        acc = q("SELECT access_until FROM users WHERE uid=?", (uid,), "one")
        return "ok" if acc and acc[0] and acc[0] > now() else "expired"
    if expires < now():
        return "expired"
    return "ok"


# ============================================================
# CLAVIERS
# ============================================================

def contact_kb(with_request=False):
    kb = types.InlineKeyboardMarkup()
    if with_request:
        kb.row(types.InlineKeyboardButton("🙋 Demander l'accès", callback_data="ac:req"))
    kb.row(
        types.InlineKeyboardButton("💬 Telegram", url=CONTACT_TG),
        types.InlineKeyboardButton("📱 WhatsApp", url=CONTACT_WA),
    )
    return kb


def _request_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🙋 Je n'ai pas de code : demander l'accès", callback_data="ac:req"))
    return kb


# ============================================================
# ACCUEIL / PORTE D'ENTRÉE
# ============================================================

def _welcome(chat_id, name, expired=False):
    if expired:
        _send(chat_id,
              "⏳ <b>%s</b>, ton accès à Téo a expiré.\n"
              "Tu peux demander une prolongation ou contacter %s 👇" % (_esc(name), _esc(OWNER_NAME)),
              reply_markup=contact_kb(with_request=True))
        return
    _send(chat_id,
          "👋 <b>Salut !</b>\n\n"
          "✨ Créé par <b>%s</b>. Je m'appelle <b>%s</b>, pour vous servir.\n"
          "🎤 À qui ai-je l'honneur, s'il vous plaît ?" % (_esc(OWNER_NAME), _esc(BOT_NAME)))
    _send(chat_id,
          "🤝 Enchanté <b>%s</b> !\n\n"
          "🔐 Pour entrer, donne-moi ton <b>code d'accès</b>, s'il te plaît." % _esc(name),
          reply_markup=_request_kb())


def _locked(chat_id, until):
    mins = max(1, int((until - now()) / 60) + 1)
    _send(chat_id,
          "🔒 Trop d'essais. Réessaie dans <b>%d min</b>.\n"
          "Besoin d'aide ? Écris directement à %s 👇" % (mins, _esc(OWNER_NAME)),
          reply_markup=contact_kb())


def _fail(message):
    uid = message.from_user.id
    n = _attempts.get(uid, 0) + 1
    _attempts[uid] = n
    name = message.from_user.first_name or "ami"
    if n >= MAX_ATTEMPTS:
        _attempts[uid] = 0
        _lock_until[uid] = now() + LOCK_SECONDS
        _gate_state.pop(uid, None)
        _locked(message.chat.id, _lock_until[uid])
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("🚫 Bloquer", callback_data="adm:ban:%d" % uid))
        _tell_owners("⚠️ <b>Alerte</b> : %s (ID <code>%d</code>) a échoué %d fois au code d'accès."
                     % (_esc(name), uid, MAX_ATTEMPTS), kb)
        return
    _send(message.chat.id,
          "❌ Code incorrect (%d/%d). Réessaie, ou contacte %s 👇" % (n, MAX_ATTEMPTS, _esc(OWNER_NAME)),
          reply_markup=contact_kb(with_request=True))


def _success(message, role, until=None):
    uid = message.from_user.id
    name = message.from_user.first_name or "ami"
    _gate_state.pop(uid, None)
    _attempts.pop(uid, None)
    _lock_until.pop(uid, None)
    _grant(message.from_user, role, until)
    if role == "owner":
        _send(message.chat.id, "👑 Bienvenue, patron. Désormais je te reconnais tout seul.")
        owner_hello(message.chat.id, name)
    elif role == "guest":
        _send(message.chat.id,
              "✅ Bienvenue <b>%s</b> ! Ton accès invité est actif jusqu'au <b>%s</b>.\n"
              "💬 Dis-moi ce que tu en penses avec /avis, ça m'aide énormément 🙏" % (_esc(name), fmt_date(until)))
    else:
        _send(message.chat.id, "✅ Bienvenue <b>%s</b> ! La porte est ouverte 🎶" % _esc(name))
    if _menu_fn:
        _menu_fn(message.chat.id, name)


def _evaluate(uid, text):
    """Retourne ('member'|'guest'|'claim'|'expired'|'other'|None, extra)."""
    n = norm_code(text)
    if n and verify_secret(n, ACCESS_CODE_HASH):
        return "member", None
    if n:
        res = _try_guest_code(uid, n)
        if res == "ok":
            return "guest", n
        if res in ("expired", "other"):
            return res, None
    p = norm_phrase(text)
    if p and verify_secret(p, OWNER_PHRASE_HASH):
        return "claim", None
    return None, None


def _gate_message(message):
    uid = message.from_user.id
    cid = message.chat.id
    name = message.from_user.first_name or "ami"
    text = (message.text or "").strip()

    if uid in _guest_view and text.lower() == "/moi":
        _guest_view.discard(uid)
        _gate_state.pop(uid, None)
        _send(cid, "👑 Retour en mode patron.")
        return

    st = _gate_state.get(uid)
    until = _lock_until.get(uid, 0)

    if text.lower().startswith("/start"):
        _gate_state[uid] = {}
        if now() < until:
            return _locked(cid, until)
        return _welcome(cid, name, expired=(status_of(uid) == "expired"))

    if now() < until:
        _del(message)
        return _locked(cid, until)

    # Étape 2 du claim propriétaire : il faut maintenant le code.
    if st and st.get("claim"):
        _del(message)
        if verify_secret(norm_code(text), ACCESS_CODE_HASH):
            return _success(message, "owner")
        return _fail(message)

    kind, extra = _evaluate(uid, text)

    if kind == "member":
        _del(message)
        return _success(message, "member")

    if kind == "guest":
        _del(message)
        t = now()
        until_ts = t + GUEST_DAYS * 86400
        row = q("SELECT access_until FROM users WHERE uid=?", (uid,), "one")
        if row and row[0] and row[0] > t:
            until_ts = row[0]
        q("UPDATE codes SET used_by=?, used_at=?, bound_uid=COALESCE(bound_uid, ?) WHERE hash=?",
          (uid, t, uid, _sha(extra)))
        return _success(message, "guest", until_ts)

    if kind == "claim":
        _del(message)
        _gate_state[uid] = {"claim": True}
        return _send(cid, "🤫 Je te reconnais… Donne-moi maintenant ton code, patron.")

    if kind in ("expired", "other"):
        _del(message)
        msg = ("⌛ Ce code a expiré." if kind == "expired"
               else "🔒 Ce code est déjà utilisé par quelqu'un d'autre.")
        return _send(cid, msg + "\nContacte %s pour en obtenir un nouveau 👇" % _esc(OWNER_NAME),
                     reply_markup=contact_kb(with_request=True))

    # Rien ne correspond.
    if st is None:
        _gate_state[uid] = {}
        return _welcome(cid, name, expired=(status_of(uid) == "expired"))
    _del(message)
    _fail(message)


# ============================================================
# DEMANDE D'ACCÈS (style « approuver les nouveaux membres »)
# ============================================================

def _on_request(call):
    u = call.from_user
    uid = u.id
    name = u.first_name or "ami"
    chat_id = call.message.chat.id
    if now() < _lock_until.get(uid, 0):
        return _answer(call, "Patiente un peu avant de réessayer.", True)
    row = q("SELECT status FROM requests WHERE uid=?", (uid,), "one")
    if row and row[0] == "pending":
        return _answer(call, "Ta demande est déjà envoyée, patiente 🙏", True)
    if not owners():
        _answer(call)
        return _send(chat_id, "📭 Je ne peux pas joindre %s pour l'instant. Écris-lui directement 👇" % _esc(OWNER_NAME),
                     reply_markup=contact_kb())
    q("INSERT INTO requests(uid, name, username, status, ts) VALUES(?,?,?,'pending',?) "
      "ON CONFLICT(uid) DO UPDATE SET name=excluded.name, username=excluded.username, status='pending', ts=excluded.ts",
      (uid, name, u.username or "", now()))
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✅ Approuver", callback_data="adm:ok:%d" % uid),
           types.InlineKeyboardButton("❌ Refuser", callback_data="adm:no:%d" % uid))
    tag = (" (@%s)" % u.username) if u.username else ""
    _tell_owners("👤 <b>%s</b>%s demande l'accès à Téo.\nID <code>%d</code>" % (_esc(name), _esc(tag), uid), kb)
    _answer(call, "Demande envoyée ✅")
    _send(chat_id, "📨 Demande envoyée à <b>%s</b>.\nTu recevras ton code ici dès qu'il l'aura validée 🙏" % _esc(OWNER_NAME))


# ============================================================
# PROPRIÉTAIRE : ACCUEIL + PANEL
# ============================================================

def owner_hello(chat_id, name):
    uid = chat_id
    q("INSERT INTO users(uid, first_name, role, banned, created, last_seen) VALUES(?,?,'owner',0,?,?) "
      "ON CONFLICT(uid) DO UPDATE SET role='owner', first_name=excluded.first_name",
      (uid, name, now(), now()))
    t = now()
    users = q("SELECT COUNT(*) FROM users WHERE banned=0 AND role IN ('member','guest')", fetch="one")[0]
    act = q("SELECT COUNT(*) FROM users WHERE last_seen>?", (t - 86400,), "one")[0]
    dl = q("SELECT COUNT(*) FROM downloads WHERE ts>?", (t - 86400,), "one")[0]
    pend = q("SELECT COUNT(*) FROM requests WHERE status='pending'", fetch="one")[0]
    text = ("👑 <b>Bon retour, patron.</b> %s est à ton service.\n"
            "📈 %d utilisateurs · %d actifs (24 h) · %d téléchargements (24 h)" % (_esc(BOT_NAME), users, act, dl))
    if pend:
        text += "\n🔔 %d demande(s) d'accès en attente" % pend
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🛠 Panel admin", callback_data="adm:home"))
    _safe_send(chat_id, text, reply_markup=kb)


def _panel_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📊 Stats", callback_data="adm:stats"),
           types.InlineKeyboardButton("👥 Utilisateurs", callback_data="adm:users"),
           types.InlineKeyboardButton("🎟 Code invité", callback_data="adm:inv"),
           types.InlineKeyboardButton("📝 Avis reçus", callback_data="adm:fb"),
           types.InlineKeyboardButton("📣 Annonce", callback_data="adm:bc"),
           types.InlineKeyboardButton("🕶 Vue invité", callback_data="adm:gv"))
    return kb


def _back_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("⬅️ Panel", callback_data="adm:home"))
    return kb


def _stats_text():
    t = now()
    one = lambda sql, a=(): q(sql, a, "one")[0]  # noqa: E731
    members = one("SELECT COUNT(*) FROM users WHERE banned=0 AND role='member'")
    guests = one("SELECT COUNT(*) FROM users WHERE banned=0 AND role='guest' AND access_until>?", (t,))
    act = one("SELECT COUNT(*) FROM users WHERE last_seen>?", (t - 86400,))
    dl_all = one("SELECT COUNT(*) FROM downloads")
    dl_24 = one("SELECT COUNT(*) FROM downloads WHERE ts>?", (t - 86400,))
    pend = one("SELECT COUNT(*) FROM requests WHERE status='pending'")
    top = q("SELECT artist, COUNT(*) c FROM downloads WHERE artist<>'' GROUP BY artist ORDER BY c DESC LIMIT 5", fetch="all")
    text = ("📊 <b>Stats Téo</b>\n\n"
            "👥 Membres : <b>%d</b> · invités actifs : <b>%d</b>\n"
            "🟢 Actifs sur 24 h : <b>%d</b>\n"
            "🎵 Téléchargements : <b>%d</b> (24 h : %d)\n"
            "🔔 Demandes en attente : <b>%d</b>\n"
            "⚠️ Erreurs depuis le démarrage : <b>%d</b>" % (members, guests, act, dl_all, dl_24, pend, _errors["count"]))
    if top:
        text += "\n\n🏆 <b>Artistes les plus téléchargés</b>\n" + "\n".join(
            "%d. %s — %d" % (i + 1, _esc(a), c) for i, (a, c) in enumerate(top))
    return text


def _users_view():
    rows = q("SELECT uid, first_name, role, banned FROM users ORDER BY last_seen DESC LIMIT 12", fetch="all")
    kb = types.InlineKeyboardMarkup(row_width=1)
    for uid, name, role, banned in rows:
        icon = "🚫" if banned else {"owner": "👑", "member": "✅", "guest": "⏳"}.get(role, "•")
        kb.add(types.InlineKeyboardButton(("%s %s · %s" % (icon, name or uid, role))[:60],
                                          callback_data="adm:u:%d" % uid))
    kb.add(types.InlineKeyboardButton("⬅️ Panel", callback_data="adm:home"))
    return ("👥 <b>Utilisateurs</b> (les 12 plus récents)" if rows else "👥 Aucun utilisateur pour l'instant."), kb


def _user_view(uid):
    row = q("SELECT first_name, username, role, access_until, banned, dl_count, last_seen FROM users WHERE uid=?",
            (uid,), "one")
    if not row:
        return "Utilisateur introuvable.", _back_kb()
    name, uname, role, until, banned, dl, seen = row
    text = ("👤 <b>%s</b>%s\nID <code>%d</code>\nRôle : <b>%s</b>%s\n🎵 Téléchargements : %d\n🕒 Dernière activité : %s"
            % (_esc(name), (" (@%s)" % _esc(uname)) if uname else "", uid, role,
               (" · accès jusqu'au %s" % fmt_date(until)) if role == "guest" else "", dl, fmt_date(seen)))
    if banned:
        text += "\n🚫 <b>Bloqué</b>"
    kb = types.InlineKeyboardMarkup(row_width=2)
    if role != "owner":
        kb.add(types.InlineKeyboardButton("✅ Débloquer" if banned else "🚫 Bloquer",
                                          callback_data=("adm:unb:%d" if banned else "adm:ban:%d") % uid),
               types.InlineKeyboardButton("⛔ Révoquer l'accès", callback_data="adm:rev:%d" % uid))
        if role == "guest":
            kb.add(types.InlineKeyboardButton("➕ %d jours" % GUEST_DAYS, callback_data="adm:ext:%d" % uid))
    kb.add(types.InlineKeyboardButton("⬅️ Utilisateurs", callback_data="adm:users"))
    return text, kb


def _edit(call, text, kb=None):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode="HTML", reply_markup=kb)
    except Exception:
        _safe_send(call.message.chat.id, text, reply_markup=kb)


def _broadcast(sender, text):
    t = now()
    rows = q("SELECT uid, first_name FROM users WHERE banned=0 AND uid<>? AND "
             "(role IN ('member','owner') OR (role='guest' AND access_until>?))", (sender, t), "all")
    sent = 0
    for uid, name in rows:
        body = "📣 " + _esc(text).replace("{prenom}", _esc(name or "ami"))
        if _safe_send(uid, body):
            sent += 1
        time.sleep(0.05)
    return sent, len(rows)


def _on_admin(call):
    uid = call.from_user.id
    if not _owner_identity(uid):
        return _answer(call)
    data = call.data
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    target = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else None
    _answer(call)

    if action == "home":
        return _edit(call, "🛠 <b>Panel admin</b>", _panel_kb())
    if action == "stats":
        return _edit(call, _stats_text(), _back_kb())
    if action == "users":
        text, kb = _users_view()
        return _edit(call, text, kb)
    if action == "u" and target:
        text, kb = _user_view(target)
        return _edit(call, text, kb)
    if action == "inv":
        code = new_guest_code()
        return _edit(call,
                     "🎟 <b>Nouveau code invité</b>\n\n<code>%s</code>\n\n"
                     "• À activer dans les %d jours\n• Utilisable une seule fois (lié au premier compte qui l'entre)\n"
                     "• Donne ensuite %d jours d'accès" % (code, GUEST_DAYS, GUEST_DAYS), _back_kb())
    if action == "fb":
        rows = q("SELECT name, plus, moins, note, ts FROM feedback ORDER BY id DESC LIMIT 5", fetch="all")
        if not rows:
            return _edit(call, "📝 Aucun avis pour l'instant.", _back_kb())
        text = "📝 <b>Derniers avis</b>\n"
        for name, plus, moins, note, ts in rows:
            text += "\n<b>%s</b> · %s/5 · %s\n👍 %s\n👎 %s\n" % (_esc(name), note, fmt_date(ts), _esc(plus), _esc(moins))
        return _edit(call, text[:3900], _back_kb())
    if action == "bc":
        _bc_wait.add(uid)
        return _edit(call, "📣 <b>Annonce</b>\nÉcris ton message (tu peux utiliser {prenom}). /annuler pour sortir.", _back_kb())
    if action == "bcgo":
        text = _bc_draft.pop(uid, None)
        if not text:
            return _edit(call, "Aucune annonce en attente.", _back_kb())
        _edit(call, "📤 Envoi en cours…")
        sent, total = _broadcast(uid, text)
        return _edit(call, "✅ Annonce envoyée à %d/%d personnes." % (sent, total), _back_kb())
    if action == "bcno":
        _bc_draft.pop(uid, None)
        _bc_wait.discard(uid)
        return _edit(call, "Annonce annulée.", _back_kb())
    if action == "gv":
        _guest_view.add(uid)
        _gate_state.pop(uid, None)
        _edit(call, "🕶 <b>Vue invité activée.</b> Tu vois Téo comme un inconnu. Tape /moi pour revenir.")
        return _welcome(call.message.chat.id, call.from_user.first_name or "ami")

    if action in ("ban", "unb", "rev", "ext") and target:
        if _owner_identity(target):
            return _answer(call, "Impossible sur un propriétaire.", True)
        if action == "ban":
            q("INSERT INTO users(uid, first_name, role, banned, created, last_seen) VALUES(?,?,'none',1,?,?) "
              "ON CONFLICT(uid) DO UPDATE SET banned=1", (target, "", now(), now()))
        elif action == "unb":
            q("UPDATE users SET banned=0 WHERE uid=?", (target,))
        elif action == "rev":
            q("UPDATE users SET role='none', access_until=NULL WHERE uid=?", (target,))
        elif action == "ext":
            q("UPDATE users SET access_until=MAX(COALESCE(access_until,0), ?) + ?, reminded=0 WHERE uid=?",
              (now(), GUEST_DAYS * 86400, target))
        text, kb = _user_view(target)
        return _edit(call, text, kb)

    if action in ("ok", "no") and target:
        row = q("SELECT name, status FROM requests WHERE uid=?", (target,), "one")
        if not row:
            return _edit(call, "Demande introuvable.")
        name, status = row
        if status != "pending":
            return _edit(call, "ℹ️ Demande de <b>%s</b> déjà traitée (%s)." % (_esc(name), status))
        if action == "no":
            q("UPDATE requests SET status='refused' WHERE uid=?", (target,))
            _safe_send(target, "🙏 Ta demande n'a pas pu être validée pour le moment. "
                               "Tu peux contacter %s directement 👇" % _esc(OWNER_NAME), reply_markup=contact_kb())
            return _edit(call, "❌ Demande de <b>%s</b> refusée." % _esc(name))
        code = new_guest_code(bound_uid=target)
        q("UPDATE requests SET status='approved' WHERE uid=?", (target,))
        _safe_send(target,
                   "🎉 <b>Ta demande est validée !</b>\n\nVoici ton code (valable %d jours) :\n<code>%s</code>\n\n"
                   "Envoie-le-moi ici pour entrer 🎶" % (GUEST_DAYS, code))
        return _edit(call, "✅ <b>%s</b> approuvé(e).\nCode envoyé : <code>%s</code> (valable %d jours)"
                     % (_esc(name), code, GUEST_DAYS))


# ============================================================
# AVIS (/avis)
# ============================================================

def _start_feedback(chat_id, uid):
    _fb[uid] = {"step": "plus"}
    _send(chat_id, "📝 <b>Ton avis compte !</b>\n\n👍 D'abord, qu'est-ce qui t'a plu dans Téo ?\n<i>(/annuler pour sortir)</i>")


def _on_feedback_text(message):
    uid = message.from_user.id
    st = _fb.get(uid)
    text = (message.text or "").strip()[:800]
    if st["step"] == "plus":
        st.update(plus=text, step="moins")
        return _send(message.chat.id, "👎 Et qu'est-ce qui ne va pas, ou qui manque ?")
    if st["step"] == "moins":
        st.update(moins=text, step="note")
        kb = types.InlineKeyboardMarkup()
        kb.row(*[types.InlineKeyboardButton("%d ⭐" % n, callback_data="av:r:%d" % n) for n in range(1, 6)])
        return _send(message.chat.id, "⭐ Pour finir, une note sur 5 ?", reply_markup=kb)
    _send(message.chat.id, "Utilise les boutons pour donner ta note 👆")


def _on_feedback_note(call):
    uid = call.from_user.id
    st = _fb.get(uid)
    if not st or st.get("step") != "note":
        return _answer(call, "Avis expiré. Relance /avis.", True)
    note = int(call.data.split(":")[2])
    name = call.from_user.first_name or "ami"
    q("INSERT INTO feedback(uid, name, plus, moins, note, ts) VALUES(?,?,?,?,?,?)",
      (uid, name, st.get("plus", ""), st.get("moins", ""), note, now()))
    _fb.pop(uid, None)
    _answer(call, "Merci ! 🙏")
    _send(call.message.chat.id, "🙏 Merci <b>%s</b> ! Ton avis a bien été transmis à %s." % (_esc(name), _esc(OWNER_NAME)))
    _tell_owners("📝 <b>Nouvel avis de %s</b> · %d/5\n👍 %s\n👎 %s"
                 % (_esc(name), note, _esc(st.get("plus")), _esc(st.get("moins"))))


# ============================================================
# SESSION D'ÉCOUTE : « Ça sera tout pour aujourd'hui »
# ============================================================

_BYE = [
    "🎧 Ça sera tout pour aujourd'hui, <b>{n}</b> ! Merci d'avoir passé du temps avec Téo. À très vite 👋",
    "✨ C'est tout bon pour aujourd'hui, <b>{n}</b> ! Tes sons sont prêts, profite bien 🔥",
    "🎶 <b>{n}</b>, on s'arrête là pour aujourd'hui ! Téo reste dispo dès que tu veux 🤝",
    "🌙 Ça sera tout pour aujourd'hui, <b>{n}</b>. Bonne écoute, et reviens quand t'as envie de nouveautés 🎵",
]


def touch(uid, name=None):
    t = now()
    with _sess_lock:
        s = _sess.setdefault(uid, {"count": 0, "last": t, "notified": False, "seen": 0})
        if s["notified"]:
            s["count"], s["notified"] = 0, False
        s["last"] = t
        if name:
            s["name"] = name
        write = t - s["seen"] > 60
        if write:
            s["seen"] = t
    if write:
        try:
            q("UPDATE users SET last_seen=? WHERE uid=?", (t, uid))
        except Exception:
            log.warning("last_seen non enregistré", exc_info=True)


def on_download(uid, tr):
    t = now()
    with _sess_lock:
        s = _sess.setdefault(uid, {"count": 0, "last": t, "notified": False, "seen": 0})
        s["count"] += 1
        s["last"] = t
        s["notified"] = False
    try:
        q("INSERT INTO downloads(uid, artist, title, ts) VALUES(?,?,?,?)",
          (uid, (tr.get("artist") or "")[:100], (tr.get("title") or "")[:200], t))
        q("UPDATE users SET dl_count=dl_count+1 WHERE uid=?", (uid,))
    except Exception:
        log.warning("Historique non enregistré", exc_info=True)


def note_error():
    _errors["count"] += 1


def _bye(uid, name):
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🎵 Un dernier son ?", callback_data="ac:last"))
    _safe_send(uid, random.choice(_BYE).format(n=_esc(name)), reply_markup=kb)


_last_reminder = {"t": 0}


def _tick():
    t = now()
    due = []
    with _sess_lock:
        for uid, s in _sess.items():
            if s["count"] >= SESSION_MIN_DL and not s["notified"] and t - s["last"] >= SESSION_IDLE:
                due.append((uid, s.get("name")))
    for uid, name in due:
        if _busy_fn and _busy_fn(uid):
            continue
        with _sess_lock:
            _sess[uid]["notified"] = True
        _bye(uid, name or _first_name(uid))

    if t - _last_reminder["t"] > 600:
        _last_reminder["t"] = t
        rows = q("SELECT uid, first_name FROM users WHERE role='guest' AND banned=0 AND reminded=0 "
                 "AND access_until>? AND access_until<?", (t, t + 86400), "all")
        for uid, name in rows:
            q("UPDATE users SET reminded=1 WHERE uid=?", (uid,))
            _safe_send(uid, "⏳ <b>%s</b>, ton accès à Téo expire d'ici 24 h.\n"
                            "Un petit /avis avant ? Ça m'aide beaucoup 🙏" % _esc(name or "ami"))


def _loop():
    while True:
        time.sleep(5)
        try:
            _tick()
        except Exception:
            log.warning("Boucle de session en échec", exc_info=True)


def start():
    threading.Thread(target=_loop, daemon=True, name="extras-session").start()


# ============================================================
# INSTALLATION DES HANDLERS (à appeler AVANT ceux de main.py)
# ============================================================

def _uid(m):
    return m.from_user.id if getattr(m, "from_user", None) else 0


def _is_private(m):
    chat = m.chat if hasattr(m, "chat") else m.message.chat
    return chat.type == "private"


def install(tele_bot, esc=None, menu=None, busy=None):
    """menu(chat_id, name) affiche le menu principal ; busy(chat_id) -> bool si des tâches tournent."""
    global bot, _menu_fn, _busy_fn, _esc
    bot, _menu_fn, _busy_fn = tele_bot, menu, busy
    if esc:
        _esc = esc
    _db()

    # --- Messages -------------------------------------------------------
    @bot.message_handler(content_types=["text"], func=lambda m: not _is_private(m))
    def _ignore_groups(m):          # bot strictement privé
        pass

    @bot.message_handler(content_types=["text"], func=lambda m: status_of(_uid(m)) == "banned")
    def _ignore_banned(m):
        pass

    @bot.message_handler(content_types=["text"], func=lambda m: not is_ok(_uid(m)))
    def _gate(m):
        try:
            _gate_message(m)
        except Exception:
            log.exception("Porte d'entrée en échec")

    @bot.message_handler(commands=["admin"], func=lambda m: is_owner(_uid(m)))
    def _admin(m):
        _safe_send(m.chat.id, "🛠 <b>Panel admin</b>", reply_markup=_panel_kb())

    @bot.message_handler(commands=["avis"])
    def _avis(m):
        _start_feedback(m.chat.id, _uid(m))

    @bot.message_handler(commands=["annuler"])
    def _cancel(m):
        uid = _uid(m)
        _fb.pop(uid, None)
        _bc_wait.discard(uid)
        _bc_draft.pop(uid, None)
        _safe_send(m.chat.id, "👌 C'est annulé.")

    @bot.message_handler(commands=["stop"])
    def _stop(m):
        q("UPDATE users SET notif_off=1 WHERE uid=?", (_uid(m),))
        _safe_send(m.chat.id, "🔕 C'est noté : plus de suggestions. /reprendre pour les réactiver.")

    @bot.message_handler(commands=["reprendre"])
    def _resume(m):
        q("UPDATE users SET notif_off=0 WHERE uid=?", (_uid(m),))
        _safe_send(m.chat.id, "🔔 Suggestions réactivées.")

    @bot.message_handler(content_types=["text"],
                         func=lambda m: _uid(m) in _bc_wait and not (m.text or "").startswith("/"))
    def _bc_text(m):
        uid = _uid(m)
        _bc_draft[uid] = (m.text or "").strip()[:1500]
        _bc_wait.discard(uid)
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("✅ Envoyer", callback_data="adm:bcgo"),
               types.InlineKeyboardButton("✖ Annuler", callback_data="adm:bcno"))
        _safe_send(m.chat.id, "📣 <b>Aperçu</b>\n\n%s" % _esc(_bc_draft[uid]), reply_markup=kb)

    @bot.message_handler(content_types=["text"],
                         func=lambda m: _uid(m) in _fb and not (m.text or "").startswith("/"))
    def _fb_text(m):
        _on_feedback_text(m)

    # --- Boutons --------------------------------------------------------
    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("adm:"))
    def _cb_admin(c):
        try:
            _on_admin(c)
        except Exception:
            log.exception("Panel admin en échec")
            _answer(c, "Erreur, regarde les logs.", True)

    @bot.callback_query_handler(func=lambda c: c.data == "ac:req" and not is_ok(c.from_user.id))
    def _cb_request(c):
        try:
            _on_request(c)
        except Exception:
            log.exception("Demande d'accès en échec")
            _answer(c, "Erreur, réessaie.", True)

    @bot.callback_query_handler(func=lambda c: not is_ok(c.from_user.id))
    def _cb_denied(c):
        _answer(c, "🔐 Accès requis. Envoie /start.", True)

    @bot.callback_query_handler(func=lambda c: c.data == "ac:last")
    def _cb_last(c):
        _answer(c)
        touch(c.from_user.id, c.from_user.first_name)
        _safe_send(c.message.chat.id, "🎤 Vas-y <b>%s</b>, écris le titre ou l'artiste…" % _esc(c.from_user.first_name or "ami"))

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("av:"))
    def _cb_feedback(c):
        if c.data == "av:start":
            _answer(c)
            return _start_feedback(c.message.chat.id, c.from_user.id)
        _on_feedback_note(c)
