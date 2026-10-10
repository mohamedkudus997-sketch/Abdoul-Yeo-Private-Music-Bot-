"""Bannières d'images : une image en haut du message, le texte en légende, les boutons dessous.
Chaque image est envoyée une seule fois à Telegram (file_id gardé ensuite). Si l'image manque
ou si l'envoi échoue, le message part en texte seul : rien ne casse."""
import logging
import os
import threading

log = logging.getLogger("banners")

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ON = os.environ.get("BANNERS", "1") != "0"
CAPTION_MAX = 1024
_ids = {}
_lock = threading.Lock()


def path(key):
    p = os.path.join(DIR, key + ".jpg")
    return p if os.path.isfile(p) else None


def available():
    return sorted(f[:-4] for f in os.listdir(DIR) if f.endswith(".jpg")) if os.path.isdir(DIR) else []


def send(bot, chat_id, key, text, **kw):
    """Envoie la bannière `key` avec `text` en légende ; sinon texte seul. Retourne le message."""
    kw.setdefault("parse_mode", "HTML")
    fp = path(key) if (ON and key) else None
    if fp and len(text) <= CAPTION_MAX:
        try:
            with _lock:
                fid = _ids.get(key)
            if fid:
                msg = _photo(bot, chat_id, fid, text, kw)
            else:
                with open(fp, "rb") as fh:
                    msg = _photo(bot, chat_id, fh, text, kw)
                try:
                    with _lock:
                        _ids[key] = msg.photo[-1].file_id
                except Exception:
                    pass
            return msg
        except Exception:
            log.info("Bannière « %s » non envoyée, repli texte.", key, exc_info=True)
            with _lock:
                _ids.pop(key, None)
    try:
        return bot.send_message(chat_id, text, **kw)
    except Exception:
        if "message_effect_id" not in kw:
            raise
        kw = dict(kw)
        kw.pop("message_effect_id")
        return bot.send_message(chat_id, text, **kw)


def _photo(bot, chat_id, photo, text, kw):
    try:
        return bot.send_photo(chat_id, photo, caption=text, **kw)
    except Exception:
        if "message_effect_id" not in kw:
            raise
        k = dict(kw)
        k.pop("message_effect_id")
        if hasattr(photo, "seek"):
            photo.seek(0)
        return bot.send_photo(chat_id, photo, caption=text, **k)
