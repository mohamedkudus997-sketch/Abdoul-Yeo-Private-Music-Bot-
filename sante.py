"""Santé de Téo : suivi des échecs YouTube, alertes au patron, état des cookies, texte de /sante.

Rien ici ne parle à YouTube : le module ne fait que compter, surveiller et prévenir.
"""
import logging
import re
import threading
import time

log = logging.getLogger("musicbot")

STARTED = time.time()
FAIL_ALERT_AFTER = 3            # échecs YouTube d'affilée avant d'alerter le patron
FAIL_ALERT_EVERY = 3 * 3600     # au plus une alerte « échecs » toutes les 3 h
COOKIE_ALERT_EVERY = 24 * 3600  # au plus une alerte « cookies » par jour
CHECK_EVERY = 6 * 3600

_state = {"last_ok": None, "fails": 0, "last_error": "", "alert_fail": 0, "alert_cookie": 0}
_lock = threading.Lock()
_tell = {"fn": None, "info": None}

# Messages d'erreur qui viennent de YouTube / du serveur (et pas d'un titre introuvable ou d'un lien invalide).
_YT_ERR = re.compile(r"bloque|session|refus|trop de temps|n'a pas pu|cookies|connexion|403", re.I)


def note_success():
    with _lock:
        _state["last_ok"] = time.time()
        _state["fails"] = 0


def note_failure(message):
    """Appelé à chaque échec utilisateur. N'alerte que pour les échecs côté YouTube."""
    text = str(message or "")
    if not _YT_ERR.search(text):
        return
    with _lock:
        _state["fails"] += 1
        _state["last_error"] = text[:200]
        due = (_state["fails"] >= FAIL_ALERT_AFTER
               and time.time() - _state["alert_fail"] > FAIL_ALERT_EVERY)
        if due:
            _state["alert_fail"] = time.time()
        fails = _state["fails"]
    if due:
        _alert("🚨 <b>Téléchargements en échec</b>\n"
               "<blockquote>%d échecs d'affilée.\nDernière erreur : <i>%s</i></blockquote>\n"
               "Pistes : envoie un fichier <code>cookies.txt</code> tout neuf ici, "
               "ou ajoute un proxy (variable <code>YTDLP_PROXY</code>). Détails : /sante"
               % (fails, _esc(text[:160])))


def fails():
    return _state["fails"]


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _alert(text):
    fn = _tell["fn"]
    if not fn:
        return
    try:
        fn(text)
    except Exception:
        log.warning("Alerte santé non envoyée", exc_info=True)


def ago(ts):
    if not ts:
        return "jamais"
    s = max(0, int(time.time() - ts))
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return "il y a %d min" % (s // 60)
    if s < 86400:
        return "il y a %d h %02d" % (s // 3600, (s % 3600) // 60)
    return "il y a %d j" % (s // 86400)


def uptime():
    s = int(time.time() - STARTED)
    if s < 3600:
        return "%d min" % max(1, s // 60)
    if s < 86400:
        return "%d h %02d" % (s // 3600, (s % 3600) // 60)
    return "%d j %d h" % (s // 86400, (s % 86400) // 3600)


def days_left(min_exp):
    if not min_exp:
        return None
    return int((min_exp - time.time()) // 86400)


def snapshot(info):
    """Même contenu que /sante, sous forme de données pour la Mini application."""
    left = days_left(info.get("min_exp"))
    problems = []
    if info.get("has_cookies") and not info.get("logged_in"):
        problems.append("cookies sans connexion Google")
    if left is not None and left <= 7:
        problems.append("cookies bientôt périmés")
    if info.get("cookies_refused"):
        problems.append("cookies refusés par YouTube")
    if _state["fails"] >= FAIL_ALERT_AFTER:
        problems.append("%d échecs d'affilée" % _state["fails"])
    return {
        "ok": not problems, "problems": problems,
        "last_ok": ago(_state["last_ok"]), "fails": _state["fails"],
        "cookies": ("%d lus" % info.get("cookie_lines", 0)) if info.get("has_cookies") else "aucun",
        "logged_in": bool(info.get("logged_in")), "days_left": left,
        "source": info.get("cookie_source") or "—", "refused": bool(info.get("cookies_refused")),
        "proxy": bool(info.get("proxy")), "ytdlp": info.get("ytdlp") or "?", "js": info.get("js") or "aucun",
        "uptime": uptime(), "mem": info.get("mem") or "?", "backup": info.get("backup") or "—",
        "last_error": _state["last_error"],
    }


def health_text(info):
    """info : dictionnaire fourni par main.health_info()."""
    left = days_left(info.get("min_exp"))
    if not info.get("has_cookies"):
        cookies = "aucun cookie chargé"
    else:
        cookies = "%d lus · connexion %s" % (info.get("cookie_lines", 0), "oui" if info.get("logged_in") else "NON")
        if left is not None:
            cookies += " · expirent dans %d j" % max(0, left) if left > 0 else " · EXPIRÉS"
    problems = []
    if info.get("has_cookies") and not info.get("logged_in"):
        problems.append("cookies sans connexion Google")
    if left is not None and left <= 7:
        problems.append("cookies bientôt périmés")
    if info.get("cookies_refused"):
        problems.append("cookies refusés par YouTube")
    if _state["fails"] >= FAIL_ALERT_AFTER:
        problems.append("%d échecs d'affilée" % _state["fails"])
    verdict = ("🟢 <b>Tout est en ordre</b>" if not problems
               else "🟠 <b>À surveiller</b> : " + _esc(" · ".join(problems)))
    lines = [
        "<b>Santé de Téo</b>",
        verdict,
        "",
        "<b>YouTube</b>",
        "<blockquote>"
        "Dernier succès : %s\n"
        "Échecs d'affilée : %d\n"
        "Cookies : %s\n"
        "Source des cookies : %s\n"
        "Refus récent : %s\n"
        "Proxy : %s</blockquote>" % (
            ago(_state["last_ok"]), _state["fails"], _esc(cookies), _esc(info.get("cookie_source") or "—"),
            "oui" if info.get("cookies_refused") else "non", "actif" if info.get("proxy") else "non"),
        "",
        "<b>Moteur</b>",
        "<blockquote>"
        "yt-dlp %s · JS : %s\n"
        "Délai max par titre : %d s\n"
        "En ligne depuis : %s\n"
        "Mémoire : %s</blockquote>" % (
            _esc(info.get("ytdlp") or "?"), _esc(info.get("js") or "aucun"), info.get("budget", 0),
            uptime(), _esc(info.get("mem") or "?")),
        "",
        "<b>Sauvegarde</b>",
        "<blockquote>%s</blockquote>" % _esc(info.get("backup") or "—"),
    ]
    if _state["last_error"]:
        lines += ["", "<b>Dernière erreur</b>", "<blockquote>%s</blockquote>" % _esc(_state["last_error"])]
    return "\n".join(lines)


def _check_cookies(info_fn):
    info = info_fn()
    if not info.get("has_cookies"):
        return
    left = days_left(info.get("min_exp"))
    if left is None or left > 7:
        return
    with _lock:
        if time.time() - _state["alert_cookie"] < COOKIE_ALERT_EVERY:
            return
        _state["alert_cookie"] = time.time()
    if left <= 0:
        _alert("🍪 <b>Cookies expirés</b>\n<blockquote>Les cookies YouTube ont atteint leur date de fin.</blockquote>\n"
               "Exporte un fichier tout neuf et envoie-le-moi ici : je le prends tout de suite.")
    else:
        _alert("🍪 <b>Cookies bientôt périmés</b>\n<blockquote>Il reste environ %d jour(s).</blockquote>\n"
               "Prépare un nouveau <code>cookies.txt</code> et envoie-le-moi ici quand tu veux." % left)


def install(tell_fn, info_fn):
    """tell_fn(texte) prévient les patrons ; info_fn() renvoie l'état des cookies/moteur."""
    _tell["fn"] = tell_fn
    _tell["info"] = info_fn

    def loop():
        time.sleep(150)
        while True:
            try:
                _check_cookies(info_fn)
            except Exception:
                log.warning("Contrôle des cookies échoué", exc_info=True)
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, daemon=True, name="sante").start()
