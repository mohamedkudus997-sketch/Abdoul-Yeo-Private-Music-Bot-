"""Sauvegarde gratuite et durable : base + mémoire des titres sont envoyées dans ton chat privé avec le bot
(message épinglé). Au redémarrage de Render (disque effacé), tout est restauré automatiquement.
Réglage : BACKUP_CHAT_ID (ton identifiant Telegram numérique), sinon le premier de OWNER_IDS.
"""
import hashlib
import io
import logging
import os
import tempfile
import threading
import time
import zipfile

import extras

log = logging.getLogger("musicbot")
INTERVAL = max(2, int(os.environ.get("BACKUP_MINUTES", "5") or 5)) * 60
PREFIX = "teo_backup"
# Cookies envoyés au bot par Telegram : gardés à côté de la base, et dans la sauvegarde (BACKUP_COOKIES=0 pour les exclure).
COOKIE_UP = os.path.join(os.path.dirname(os.path.abspath(extras.DB_FILE)), "cookies_envoyes.txt")
WITH_COOKIES = os.environ.get("BACKUP_COOKIES", "1") != "0"
_last = {"digest": None, "msg": None}
_ctx = {}


def chat_id():
    raw = os.environ.get("BACKUP_CHAT_ID", "").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    owners = sorted(extras.OWNER_IDS)
    return owners[0] if owners else None


def make_zip(cache_json):
    """Octets du zip (base + cache) ; None si la base est vide/inaccessible."""
    tmp = tempfile.mkdtemp(prefix="bk_")
    try:
        path = os.path.join(tmp, "db")
        extras.backup_db(path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(path, "db")
            zf.writestr("cache.json", cache_json or "{}")
            if WITH_COOKIES and os.path.isfile(COOKIE_UP):
                zf.write(COOKIE_UP, "cookies.txt")
        return buf.getvalue()
    finally:
        for name in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def restore(bot, db_file, cache_file):
    """Au démarrage : si le disque est vide, récupère la dernière sauvegarde épinglée."""
    cid = chat_id()
    if not cid or os.path.exists(db_file):
        return False
    try:
        pm = getattr(bot.get_chat(cid), "pinned_message", None)
        doc = getattr(pm, "document", None)
        if not doc or not str(doc.file_name or "").startswith(PREFIX):
            return False
        blob = bot.download_file(bot.get_file(doc.file_id).file_path)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            with open(db_file, "wb") as fh:
                fh.write(zf.read("db"))
            if "cache.json" in zf.namelist() and not os.path.exists(cache_file):
                with open(cache_file, "wb") as fh:
                    fh.write(zf.read("cache.json"))
            if WITH_COOKIES and "cookies.txt" in zf.namelist() and not os.path.exists(COOKIE_UP):
                with open(COOKIE_UP, "wb") as fh:
                    fh.write(zf.read("cookies.txt"))
        _last["msg"] = pm.message_id
        log.info("Sauvegarde restaurée depuis Telegram.")
        return True
    except Exception:
        log.warning("Restauration impossible", exc_info=True)
        return False


def run_once(bot, cache_json_fn, force=False):
    cid = chat_id()
    if not cid:
        return False
    blob = make_zip(cache_json_fn())
    digest = hashlib.sha256(blob).hexdigest()
    if digest == _last["digest"] and not force:
        return False
    name = "%s_%s.zip" % (PREFIX, time.strftime("%Y%m%d_%H%M", time.gmtime()))
    caption = "🗄️ Sauvegarde automatique de Téo (ne pas supprimer)"
    if not _last["msg"]:
        try:                                          # après un redémarrage : on reprend le message déjà épinglé
            pm = getattr(bot.get_chat(cid), "pinned_message", None)
            pdoc = getattr(pm, "document", None)
            if pdoc and str(pdoc.file_name or "").startswith(PREFIX):
                _last["msg"] = pm.message_id
        except Exception:
            pass
    if _last["msg"]:
        try:
            from telebot import types
            fresh = io.BytesIO(blob)
            fresh.name = name
            bot.edit_message_media(types.InputMediaDocument(fresh, caption=caption), cid, _last["msg"])
            _last["digest"] = digest
            return True
        except Exception:
            log.info("Remplacement du fichier épinglé impossible : nouveau message.", exc_info=True)
    doc = io.BytesIO(blob)
    doc.name = name
    sent = bot.send_document(cid, doc, caption=caption, disable_notification=True)
    try:
        bot.pin_chat_message(cid, sent.message_id, disable_notification=True)
    except Exception:
        log.warning("Épinglage impossible", exc_info=True)
    old = _last["msg"]
    _last.update(digest=digest, msg=sent.message_id)
    if old and old != sent.message_id:
        try:
            bot.delete_message(cid, old)
        except Exception:
            pass
    return True


def start(bot, cache_json_fn):
    if not chat_id():
        log.info("Sauvegarde Telegram désactivée (renseigne BACKUP_CHAT_ID ou OWNER_IDS).")
        return

    _ctx["args"] = (bot, cache_json_fn)

    def loop():
        time.sleep(90)
        while True:
            try:
                run_once(bot, cache_json_fn)
            except Exception:
                log.warning("Sauvegarde échouée", exc_info=True)
            time.sleep(INTERVAL)

    threading.Thread(target=loop, daemon=True, name="backup").start()
    log.info("Sauvegarde Telegram active (toutes les %d min).", INTERVAL // 60)


def save_soon():
    """Sauvegarde tout de suite (ex. juste après un déblocage), sans bloquer le bot."""
    args = _ctx.get("args")
    if not args:
        return

    def go():
        try:
            run_once(*args)
        except Exception:
            log.warning("Sauvegarde immédiate échouée", exc_info=True)

    threading.Thread(target=go, daemon=True, name="backup-now").start()
