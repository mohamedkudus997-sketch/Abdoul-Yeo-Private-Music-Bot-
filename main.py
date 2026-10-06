# -*- coding: utf-8 -*-
"""
MusicBot V4 « Téo » — Telegram + Render
- Recherche du VRAI artiste (iTunes) : « mhd » -> fiche du MHD célèbre, pas un homonyme
- « artiste titre » -> téléchargement direct quand le titre correspond clairement
- Choix de la source YouTube par score (durée, chaîne Topic/officielle, pas de remix/live)
- Accueil de Téo avec code, accès invité 7 jours avec approbation, panel admin (extras.py)
- Cache file_id, file d'attente par utilisateur, albums complets
Variables Render : BOT_TOKEN (obligatoire), YT_COOKIES, OWNER_IDS,
                   ACCESS_CODE_HASH, OWNER_PHRASE_HASH, DB_FILE (optionnels)
"""

import difflib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from tempfile import mkdtemp
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import telebot
from flask import Flask
from telebot import apihelper, types
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import backup
import extras
import finder

# ============================================================
# CONFIGURATION
# ============================================================

# Laisse vide sur Render : utilise la variable d'environnement BOT_TOKEN.
# (Pydroid uniquement : colle ton token ici, jamais dans un code partagé.)
MY_BOT_TOKEN = ""

TOKEN = os.environ.get("BOT_TOKEN", "").strip() or MY_BOT_TOKEN.strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN manquant (variable d'environnement Render).")


def env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


PORT = env_int("PORT", 10000)
MAX_FILE_MB = env_int("MAX_FILE_MB", 49)
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_DURATION = env_int("MAX_DURATION", 900, 30)
MAX_ALBUM_TRACKS = env_int("MAX_ALBUM_TRACKS", 30)
WORKERS = env_int("WORKERS", 4)
TREND_COUNTRY = os.environ.get("TREND_COUNTRY", "CI").upper()[:2]
MAX_DL = env_int("MAX_DL", 2)                    # téléchargements yt-dlp simultanés (RAM limitée sur Render gratuit)
ALBUM_AHEAD = env_int("ALBUM_AHEAD", 2)          # pistes préparées d'avance pendant un album
ALBUM_TRACK_TIMEOUT = env_int("ALBUM_TRACK_TIMEOUT", 300, 30)
MAX_PENDING = env_int("MAX_PENDING", 4)      # tâches simultanées par utilisateur
ITUNES_COUNTRY = os.environ.get("ITUNES_COUNTRY", "FR").upper()
CACHE_FILE = os.environ.get("CACHE_FILE", "audio_cache.json")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
log = logging.getLogger("musicbot")

# Uploads lents sur Render : les timeouts par défaut (30 s) font échouer les envois.
apihelper.CONNECT_TIMEOUT = 20
apihelper.READ_TIMEOUT = 180
apihelper.RETRY_ON_ERROR = True
apihelper.MAX_RETRIES = 3

app = Flask(__name__)
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)

executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="job")
album_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="album")
DL_SEM = threading.BoundedSemaphore(MAX_DL)

FFMPEG = shutil.which("ffmpeg")
URL_RE = re.compile(r"^https?://\S+$", re.I)
AUDIO_EXTS = {".m4a", ".mp3", ".aac", ".ogg", ".opus", ".flac", ".wav", ".webm"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


class BotError(Exception):
    """Erreur dont le message peut être montré à l'utilisateur."""


# ============================================================
# ÉTAT / FILE D'ATTENTE
# ============================================================

state_lock = threading.Lock()
states = {}


def set_state(chat_id, **values):
    with state_lock:
        cur = states.get(chat_id, {})
        cur.update(values)
        states[chat_id] = cur
        if len(states) > 5000:
            for key in list(states)[:1000]:
                if key != chat_id:
                    states.pop(key, None)


def get_state(chat_id):
    with state_lock:
        return dict(states.get(chat_id, {}))


def clear_state(chat_id):
    with state_lock:
        states.pop(chat_id, None)


pending_lock = threading.Lock()
pending = {}


def submit(chat_id, fn, *args):
    """Met une tâche en file. Refuse seulement au-delà de MAX_PENDING par utilisateur."""
    with pending_lock:
        if pending.get(chat_id, 0) >= MAX_PENDING:
            return False
        pending[chat_id] = pending.get(chat_id, 0) + 1

    def run():
        try:
            fn(*args)
        except Exception:
            log.exception("Tâche en échec")
        finally:
            with pending_lock:
                left = pending.get(chat_id, 1) - 1
                if left <= 0:
                    pending.pop(chat_id, None)
                else:
                    pending[chat_id] = left

    executor.submit(run)
    return True


# ============================================================
# CACHE file_id (envoi instantané des titres déjà téléchargés)
# ============================================================

cache_lock = threading.Lock()
cache = {}


def cache_load():
    global cache
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
        log.info("Cache chargé : %d titres", len(cache))
    except Exception:
        cache = {}


def _cache_save():
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        log.warning("Sauvegarde du cache impossible", exc_info=True)


CACHE_V = "v2:"          # change pour invalider d'anciens file_id (ex. extraits de 30 s)


def cache_get(key):
    with cache_lock:
        return cache.get(CACHE_V + str(key))


def cache_set(key, file_id):
    with cache_lock:
        cache[CACHE_V + str(key)] = file_id
        _cache_save()


def cache_del(key):
    with cache_lock:
        cache.pop(CACHE_V + str(key), None)
        _cache_save()


# ============================================================
# FLASK / RENDER
# ============================================================

@app.get("/")
def home():
    return "MusicBot V4 is running", 200


@app.get("/health")
def health():
    return "ok", 200


def start_web():
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


def keep_alive():
    """Empêche Render (offre gratuite) d'endormir le service."""
    base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEPALIVE_URL")
    if not base:
        return
    url = base.rstrip("/") + "/health"
    while True:
        time.sleep(600)
        try:
            urlopen(url, timeout=15).read()
        except Exception:
            pass


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def esc(value):
    return html.escape(str(value or ""), quote=False)


def send(chat_id, text, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    return bot.send_message(chat_id, text, **kwargs)


EFFECT_PARTY = "5046509860389126442"      # 🎉 effet plein écran de Telegram (chats privés)


def send_fx(chat_id, text, effect=None, **kwargs):
    """Message avec effet plein écran si Telegram l'accepte, sinon message normal."""
    if effect:
        try:
            kwargs.setdefault("parse_mode", "HTML")
            return bot.send_message(chat_id, text, message_effect_id=effect, **kwargs)
        except Exception:
            log.info("Effet de message refusé, envoi simple.")
            kwargs.pop("parse_mode", None)
    return send(chat_id, text, **kwargs)


def edit(chat_id, message_id, text, **kwargs):
    kwargs.setdefault("parse_mode", "HTML")
    try:
        return bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception:
        return None


def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def answer(call, text="", alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=alert)
    except Exception:
        pass


def report(chat_id, status, exc):
    log.error("Échec : %s", exc, exc_info=not isinstance(exc, BotError))
    extras.note_error()
    msg = str(exc) if isinstance(exc, BotError) else "Je n'ai pas pu récupérer ce titre pour le moment."
    text = "⚠️ " + esc(msg)
    cause = exc.__cause__ if isinstance(exc, BotError) and exc.__cause__ else (None if isinstance(exc, BotError) else exc)
    if cause is not None and os.environ.get("SHOW_ERROR_DETAIL", "1") == "1":
        raw = re.sub(r"\x1b\[[0-9;]*m", "", str(cause)).replace("\n", " ")
        text += "\n\n<i>Détail technique :</i> <code>%s</code>" % esc(raw[:220])
    if status is None or not edit(chat_id, status.message_id, text):
        try:
            send(chat_id, text)
        except Exception:
            pass


# ============================================================
# ITUNES
# ============================================================

def itunes(endpoint, params):
    url = "https://itunes.apple.com/%s?%s" % (endpoint, urlencode(params))
    req = Request(url, headers={"User-Agent": "MusicBotV4/4.0"})
    last = None
    for _ in range(2):
        try:
            with urlopen(req, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise last


def plain(value):
    """Minuscules, sans accents ni ponctuation : sert à comparer noms et titres."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def norm(item):
    ms = item.get("trackTimeMillis")
    return {
        "id": str(item.get("trackId") or ""),
        "title": item.get("trackName") or "Titre inconnu",
        "artist": item.get("artistName") or "Artiste inconnu",
        "artist_id": str(item.get("artistId") or ""),
        "album": item.get("collectionName"),
        "album_id": str(item["collectionId"]) if item.get("collectionId") else None,
        "year": (item.get("releaseDate") or "")[:4],
        "dur": int(ms / 1000) if ms else None,
        "art": item.get("artworkUrl100"),
    }


def search_tracks(query, limit=8):
    data = itunes("search", {
        "term": query, "media": "music", "entity": "song",
        "limit": 25, "country": ITUNES_COUNTRY,
    })
    out, seen = [], set()
    for item in data.get("results") or []:
        if item.get("kind") != "song" or not item.get("trackId"):
            continue
        tr = norm(item)
        key = (tr["title"].lower(), tr["artist"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(tr)
        if len(out) >= limit:
            break
    return out


def find_artists(query):
    """Artistes iTunes classés par pertinence (le plus célèbre sort en premier)."""
    data = itunes("search", {
        "term": query, "media": "music", "entity": "musicArtist",
        "limit": 8, "country": ITUNES_COUNTRY,
    })
    out = []
    for item in data.get("results") or []:
        if item.get("artistId") and item.get("artistName"):
            out.append({
                "id": str(item["artistId"]), "name": item["artistName"],
                "genre": item.get("primaryGenreName") or "",
            })
    return out


def artist_top(artist_id, limit=12):
    """(nom, genre, titres) d'un artiste : titres dans l'ordre de popularité iTunes."""
    data = itunes("lookup", {
        "id": artist_id, "entity": "song", "limit": limit + 8, "country": ITUNES_COUNTRY,
    })
    results = data.get("results") or []
    info = next((i for i in results if i.get("wrapperType") == "artist"), {})
    songs, seen = [], set()
    for item in results:
        if item.get("wrapperType") != "track" or item.get("kind") != "song":
            continue
        tr = norm(item)
        key = plain(tr["title"])
        if key in seen:
            continue
        seen.add(key)
        songs.append(tr)
    mine = [s for s in songs if s["artist_id"] == str(artist_id)] or songs
    return info.get("artistName") or "", info.get("primaryGenreName") or "", mine[:limit]


def artist_albums(artist_id):
    data = itunes("lookup", {
        "id": artist_id, "entity": "album", "limit": 60, "country": ITUNES_COUNTRY,
    })
    out, seen = [], set()
    for item in data.get("results") or []:
        cid, raw = item.get("collectionId"), item.get("collectionName")
        if item.get("wrapperType") != "collection" or not cid or not raw:
            continue
        kind = 2 if raw.endswith(" - Single") else 1 if raw.endswith(" - EP") else 0
        name = re.sub(r"\s+-\s+(Single|EP)$", "", raw)
        key = plain(name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": str(cid), "name": name, "kind": kind,
                    "artist": item.get("artistName") or "",
                    "year": (item.get("releaseDate") or "")[:4]})
    out.sort(key=lambda a: (a["kind"], -int(a["year"] or 0)))
    return out[:8]


def _ratio(a, b):
    return difflib.SequenceMatcher(None, plain(a), plain(b)).ratio()


def match_score(query, tr):
    """0..1 : à quel point la requête ressemble à ce titre (titre seul, « artiste titre » ou « titre artiste »)."""
    title = re.sub(r"[\(\[].*?[\)\]]", "", tr["title"])
    return max(_ratio(query, title), _ratio(query, "%s %s" % (tr["artist"], title)),
               _ratio(query, "%s %s" % (title, tr["artist"])))


def smart_search(query):
    """
    Trouve le VRAI artiste avant de proposer des titres.
      card : la requête est un nom d'artiste -> fiche artiste
      auto : « artiste titre » et un seul titre colle clairement -> téléchargement direct
      list : liste de titres (filtrée sur l'artiste quand il est reconnu)
    """
    qn = plain(query)
    songs = search_tracks(query, 25)
    try:
        arts = find_artists(query)
    except Exception:
        log.warning("Recherche d'artiste indisponible", exc_info=True)
        arts = []

    cands = {}
    for a in arts:
        cands.setdefault(a["id"], a)
    for s in songs[:5]:
        if s["artist_id"]:
            cands.setdefault(s["artist_id"], {"id": s["artist_id"], "name": s["artist"], "genre": ""})

    best, rest, exact = None, "", False
    for a in cands.values():
        if plain(a["name"]) == qn:
            best, exact = a, True
            break
    if not best:
        found = []
        for order, a in enumerate(cands.values()):
            pn = plain(a["name"])
            if not pn:
                continue
            if qn.startswith(pn + " "):
                found.append((len(pn), -order, a, qn[len(pn) + 1:]))
            elif qn.endswith(" " + pn):
                found.append((len(pn), -order, a, qn[:-len(pn) - 1]))
        if found:
            found.sort(key=lambda x: (x[0], x[1]), reverse=True)
            _, _, best, rest = found[0]

    others = [a for a in arts if not best or a["id"] != best["id"]][:5]
    if not best:
        return {"mode": "list", "songs": songs[:8], "artist": None, "others": others}

    if exact:
        # Un titre qui porte exactement ce nom, d'un autre artiste, passe avant la fiche.
        conflict = any(plain(s["title"]) == qn and s["artist_id"] != best["id"] for s in songs[:3])
        if conflict:
            return {"mode": "list", "songs": songs[:8], "artist": best, "others": others}
        return {"mode": "card", "artist": best, "others": others}

    mine = [s for s in songs if s["artist_id"] == best["id"]]
    if not mine and rest:
        try:
            mine = [s for s in search_tracks("%s %s" % (best["name"], rest), 25) if s["artist_id"] == best["id"]]
        except Exception:
            mine = []
    if not mine:
        return {"mode": "list", "songs": songs[:8], "artist": None, "others": others}

    ranked = sorted(mine, key=lambda s: -_ratio(rest, s["title"]))
    r0 = _ratio(rest, ranked[0]["title"])
    r1 = _ratio(rest, ranked[1]["title"]) if len(ranked) > 1 else 0.0
    if rest and r0 >= 0.8 and (r0 - r1) >= 0.12:
        return {"mode": "auto", "track": ranked[0], "artist": best, "others": others, "songs": ranked[:6]}
    return {"mode": "list", "songs": ranked[:8], "artist": best, "others": others}


def search_albums(query):
    data = itunes("search", {
        "term": query, "media": "music", "entity": "album",
        "limit": 30, "country": ITUNES_COUNTRY,
    })
    out, seen = [], set()
    for item in data.get("results") or []:
        cid = item.get("collectionId")
        raw = item.get("collectionName")
        if not cid or not raw:
            continue
        kind = 2 if raw.endswith(" - Single") else 1 if raw.endswith(" - EP") else 0
        name = re.sub(r"\s+-\s+(Single|EP)$", "", raw)
        key = (name.lower(), (item.get("artistName") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": str(cid), "name": name, "kind": kind,
            "artist": item.get("artistName") or "",
            "year": (item.get("releaseDate") or "")[:4],
        })
    out.sort(key=lambda a: (a["kind"], -int(a["year"] or 0)))   # albums, puis EP, puis singles
    return out[:8]


def lookup_track(track_id):
    if str(track_id).startswith("ym:"):
        return finder.get(str(track_id)) or extras.fav_row(str(track_id))     # favoris : survivent au redémarrage
    data = itunes("lookup", {"id": track_id, "country": ITUNES_COUNTRY})
    for item in data.get("results") or []:
        if item.get("wrapperType") == "track" and item.get("kind") == "song":
            return norm(item)
    return None


def album_tracks(album_id):
    data = itunes("lookup", {
        "id": album_id, "entity": "song", "limit": 200, "country": ITUNES_COUNTRY,
    })
    results = data.get("results") or []
    album = next((i for i in results if i.get("wrapperType") == "collection"), {})
    items = [i for i in results if i.get("wrapperType") == "track" and i.get("kind") == "song"]
    items.sort(key=lambda i: (i.get("discNumber", 1), i.get("trackNumber", 0)))
    return album, [norm(i) for i in items]


# ============================================================
# YT-DLP
# ============================================================

COOKIE_SRC = None


def setup_cookies():
    """YT_COOKIES (contenu Netscape) ou fichier cookies.txt -> aide contre le blocage anti-bot."""
    global COOKIE_SRC
    content = os.environ.get("YT_COOKIES", "").strip()
    if content:
        path = os.path.join(mkdtemp(prefix="ck_"), "cookies.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content + "\n")
        COOKIE_SRC = path
    elif os.path.isfile("cookies.txt"):
        COOKIE_SRC = "cookies.txt"
    if COOKIE_SRC:
        log.info("Cookies YouTube activés.")


# Profils d'accès YouTube essayés dans l'ordre (le premier qui passe gagne).
# (utiliser les cookies ?, clients YouTube à imposer ou None pour le choix par défaut de yt-dlp)
YT_PROFILES = [
    (True, None),
    (True, ["tv", "web_safari"]),
    (False, ["android_vr"]),
    (False, ["ios", "mweb"]),
]


def is_youtube(url):
    host = (urlparse(url).hostname or "").lower()
    return "youtube.com" in host or host == "youtu.be"


def ydl_base(folder, profile=(True, None)):
    use_cookies, clients = profile
    opts = {
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "concurrent_fragment_downloads": 4,
        "max_filesize": MAX_FILE_BYTES,
        "overwrites": False,
    }
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}
    if COOKIE_SRC and use_cookies:
        copy = os.path.join(folder, "cookies.txt")   # copie par appel : pas de conflit entre threads
        try:
            shutil.copy(COOKIE_SRC, copy)
            opts["cookiefile"] = copy
        except Exception:
            pass
    return opts


def friendly(exc):
    text = str(exc).lower()
    if "sign in" in text or "not a bot" in text or "confirm you" in text:
        return "YouTube bloque temporairement le serveur (cookies à renouveler ?). Réessaie plus tard."
    if "needs to be reloaded" in text:
        return "YouTube a refusé la session (cookies périmés ?). Je réessaie autrement, sinon renouvelle les cookies."
    if "unsupported url" in text:
        return "Ce site/lien n'est pas pris en charge."
    if "login" in text or "log in" in text or "rate-limit" in text or "empty media" in text:
        return "Ce site demande une connexion ou limite l'accès : lien impossible à récupérer."
    if "video unavailable" in text or "unavailable" in text or "private" in text or "removed" in text:
        return "Ce média n'est pas disponible."
    if "geo" in text or "not available in your country" in text:
        return "Ce média est bloqué dans la région du serveur."
    if "larger than max-filesize" in text or "max-filesize" in text:
        return "Le fichier dépasse la limite de %d Mo." % MAX_FILE_MB
    if "http error 403" in text or "403" in text:
        return "Le site a refusé l'accès (403)."
    if "http error 404" in text or "404" in text:
        return "Lien introuvable (404)."
    if "timed out" in text or "timeout" in text:
        return "Le site met trop de temps à répondre. Réessaie."
    return "Le service n'a pas pu récupérer ce média pour le moment."


SHORT_HOSTS = ("ift.tt", "bit.ly", "t.co", "tinyurl.com", "vm.tiktok.com", "vt.tiktok.com", "fb.watch",
               "ow.ly", "goo.gl", "is.gd", "cutt.ly", "lnkd.in", "buff.ly", "rb.gy", "shorturl.at",
               "on.soundcloud.com", "spotify.link", "amzn.to", "youtu.be")


def resolve_url(url):
    """Suit les redirections des liens raccourcis (ift.tt, bit.ly…) pour obtenir le vrai lien."""
    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in SHORT_HOSTS or host == "youtu.be":
            return url
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"})
        with urlopen(req, timeout=10) as resp:
            final = resp.geturl()
        log.info("Lien raccourci %s -> %s", url, final)
        return final or url
    except Exception:
        log.warning("Résolution du lien raccourci impossible : %s", url, exc_info=True)
        return url


def find_media(folder, allowed):
    best, best_size = None, -1
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in allowed:
            size = os.path.getsize(path)
            if size > best_size:
                best, best_size = path, size
    return best


BAD_WORDS = ("remix", "live", "cover", "sped up", "speed up", "slowed", "reverb", "nightcore",
             "karaoke", "instrumental", "8d", "bass boosted", "reaction", "mashup", "type beat")


def score_entry(entry, artist_n, title_n, expected):
    """Plus le score est haut, plus la vidéo ressemble à la piste studio officielle."""
    t = plain(entry.get("title"))
    ch = plain(entry.get("channel") or entry.get("uploader") or "")
    score = 0.0
    dur = entry.get("duration")
    if expected and dur:
        diff = abs(dur - expected)
        score += 40 if diff <= 3 else 25 if diff <= 8 else 5 if diff <= 20 else -min(diff, 300) / 3.0
    elif expected:
        score -= 10
    if ch.endswith("topic"):
        score += 30                      # chaîne auto-générée « Artiste - Topic »
    if artist_n and artist_n in ch:
        score += 20                      # chaîne de l'artiste
    if artist_n and artist_n in t:
        score += 5
    if title_n and title_n in t:
        score += 10
    for word in BAD_WORDS:
        if re.search(r"\b%s\b" % re.escape(word), t) and word not in title_n:
            score -= 50
    if "official audio" in t:
        score += 10
    if "official video" in t or "clip officiel" in t:
        score -= 5                       # intro/bruitages de clip
    return score


def tolerance(expected):
    return max(12, int(expected * 0.06))      # ~6 % de marge (min. 12 s)


def rank_candidates(query, expected, artist=None, title=None, limit=3):
    """YouTube uniquement. Retourne jusqu'à `limit` URL classées, après filtre strict de durée."""
    folder = mkdtemp(prefix="srch_")
    try:
        opts = ydl_base(folder)
        opts.pop("max_filesize", None)
        opts["extract_flat"] = True
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info("ytsearch8:%s" % query, download=False)
    except DownloadError as exc:
        log.warning("Recherche YouTube échouée : %s", exc)
        raise BotError(friendly(exc)) from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    entries = []
    for e in (info or {}).get("entries") or []:
        if e and e.get("id"):
            e["_url"] = e.get("url") or "https://www.youtube.com/watch?v=%s" % e["id"]
            entries.append(e)
    if not entries:
        raise BotError("Aucun résultat trouvé pour ce titre.")
    ok = [e for e in entries if not e.get("duration") or e["duration"] <= MAX_DURATION]
    if not ok:
        raise BotError("Les résultats dépassent la limite de %d min." % (MAX_DURATION // 60))

    artist_n = plain(artist)
    title_n = plain(re.sub(r"[\(\[].*?[\)\]]", "", title or query))
    if expected:
        tol = tolerance(expected)
        ok = [e for e in ok if e.get("duration") and abs(e["duration"] - expected) <= tol]
        if not ok:
            raise BotError("Aucune version officielle de la bonne durée n'a été trouvée.")
    else:
        ok = [e for e in ok if not e.get("duration") or e["duration"] >= 45]
        if not ok:
            raise BotError("Aucun résultat exploitable (extraits trop courts).")
    ok.sort(key=lambda e: score_entry(e, artist_n, title_n, expected), reverse=True)
    for e in ok[:limit]:
        log.info("Candidat : %s | %s | %ss (score %.0f)", e.get("title"),
                 e.get("channel") or e.get("uploader"), e.get("duration"),
                 score_entry(e, artist_n, title_n, expected))
    return [e["_url"] for e in ok[:limit]]


def ensure_m4a(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".m4a", ".mp3") or not FFMPEG:
        return path
    out = os.path.splitext(path)[0] + "_c.m4a"
    try:
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", path, "-vn", "-c:a", "aac", "-b:a", "160k", out],
            check=True, timeout=180,
        )
        os.remove(path)
        return out
    except Exception:
        log.warning("Conversion ffmpeg impossible", exc_info=True)
        return path


def probe_duration(path):
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def _download_url(url, query=None, expected=None):
    folder = mkdtemp(prefix="music_")
    try:
        info, err = None, None
        profiles = YT_PROFILES if is_youtube(url) else [(True, None)]
        for n, profile in enumerate(profiles, 1):
            opts = ydl_base(folder, profile)
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            try:
                with DL_SEM, YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                if n > 1:
                    log.info("Profil YouTube %d a fonctionné : %s", n, profile)
                err = None
                break
            except DownloadError as exc:
                err = exc
                log.warning("Profil %d/%d échoué (%s) : %s", n, len(profiles), profile, exc)
                for name in os.listdir(folder):
                    if name != "cookies.txt":
                        try:
                            os.remove(os.path.join(folder, name))
                        except OSError:
                            pass
        if err:
            raise err
        if info and info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
        if not info:
            raise BotError("Aucun résultat exploitable.")
        duration = info.get("duration")
        if duration and duration > MAX_DURATION:
            raise BotError("Ce média dure %d min, la limite est de %d min." % (duration // 60, MAX_DURATION // 60))
        path = find_media(folder, AUDIO_EXTS)
        if not path:
            raise BotError("Le fichier audio n'a pas été produit.")
        path = ensure_m4a(path)
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise BotError("Le fichier dépasse la limite de %d Mo." % MAX_FILE_MB)
        real = probe_duration(path) or duration
        if real and real < 45 and not (expected and expected < 60):
            raise BotError("Fichier trop court (%d s), probablement un extrait." % real)
        if expected and real and abs(real - expected) > max(tolerance(expected), 20):
            raise BotError("Durée incorrecte (%d s au lieu de %d s)." % (real, expected))
        duration = real or duration
        return {
            "path": path, "folder": folder, "size": size,
            "title": (info.get("track") or info.get("title") or query or "Audio")[:200],
            "artist": (info.get("artist") or info.get("creator") or info.get("uploader") or "Artiste inconnu")[:100],
            "duration": int(duration) if duration else None,
            "vid": "%s_%s" % ((info.get("extractor_key") or "x").lower(), info.get("id")),
        }
    except DownloadError as exc:
        log.warning("Téléchargement échoué (%s) : %s", url, exc)
        shutil.rmtree(folder, ignore_errors=True)
        raise BotError(friendly(exc)) from exc
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def download_audio(query=None, expected=None, url=None, artist=None, title=None):
    """Lien direct, sinon YouTube seulement (plus de SoundCloud) avec vérification de durée."""
    if url:
        return _download_url(url, query)
    search = ("%s - %s (Official Audio)" % (artist, title)) if (artist and title) else query
    last = None
    for cand in rank_candidates(search, expected, artist, title):
        try:
            return _download_url(cand, query, expected)
        except BotError as exc:
            last = exc
            log.warning("Candidat rejeté (%s) : %s", cand, exc)
    raise last or BotError("Aucune source disponible pour ce titre.")


VIDEO_HEIGHTS = (720, 480, 360)


def video_format(h):
    return ("bv*[height<=%d][ext=mp4]+ba[ext=m4a]/b[height<=%d][ext=mp4]/"
            "bv*[height<=%d]+ba/b[height<=%d]/b" % (h, h, h, h))


def ensure_mp4(path, duration):
    """Telegram lit bien le MP4 H.264 : on convertit le reste, et on compresse si c'est trop gros."""
    if not FFMPEG:
        return path
    too_big = os.path.getsize(path) > MAX_FILE_BYTES
    if path.lower().endswith(".mp4") and not too_big:
        return path
    out = os.path.splitext(path)[0] + "_t.mp4"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", path, "-vf", "scale=-2:'min(480,ih)'",
           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]
    if too_big and duration:
        target = int(MAX_FILE_MB * 0.9 * 8 * 1024)               # kbit pour toute la vidéo
        v = max(120, int(target / duration) - 96)
        cmd += ["-b:v", "%dk" % v, "-maxrate", "%dk" % int(v * 1.3), "-bufsize", "%dk" % (v * 2)]
    else:
        cmd += ["-crf", "26"]
    cmd.append(out)
    subprocess.run(cmd, check=True, timeout=900)
    os.remove(path)
    return out


def download_video(url, note=None):
    folder = mkdtemp(prefix="video_")
    try:
        profiles = YT_PROFILES if is_youtube(url) else [(True, None)]
        info, err, oversize = None, None, False
        for n, profile in enumerate(profiles, 1):
            for h in VIDEO_HEIGHTS:
                opts = ydl_base(folder, profile)
                opts["format"] = video_format(h)
                opts["merge_output_format"] = "mp4"
                try:
                    with DL_SEM, YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    err = None
                    break
                except DownloadError as exc:
                    err = exc
                    text = str(exc).lower()
                    log.warning("Vidéo : profil %d, %dp échoué : %s", n, h, exc)
                    for name in os.listdir(folder):
                        if name != "cookies.txt":
                            try:
                                os.remove(os.path.join(folder, name))
                            except OSError:
                                pass
                    if "max-filesize" in text or "larger than" in text:
                        oversize = True
                        continue                 # trop lourd : on retente en qualité inférieure
                    break                        # autre erreur : autre profil d'accès
            if info:
                break
        if not info and oversize and err and FFMPEG:
            if note:
                note("🗜️ Vidéo volumineuse, je la compresse…")
            opts = ydl_base(folder)
            opts.pop("max_filesize", None)
            opts["format"] = video_format(480)
            opts["merge_output_format"] = "mp4"
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            err = None
        if err and not info:
            raise err
        if info and info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
        if not info:
            raise BotError("Aucun résultat exploitable.")
        duration = info.get("duration")
        if duration and duration > MAX_DURATION:
            raise BotError("Cette vidéo dure %d min, la limite est de %d min." % (duration // 60, MAX_DURATION // 60))
        path = find_media(folder, VIDEO_EXTS)
        if not path:
            raise BotError("Aucun fichier vidéo exploitable.")
        if (not path.lower().endswith(".mp4") or os.path.getsize(path) > MAX_FILE_BYTES) and note:
            note("🗜️ Je prépare la vidéo pour Telegram…")
        try:
            path = ensure_mp4(path, duration)
        except Exception:
            log.warning("Conversion vidéo impossible", exc_info=True)
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise BotError("La vidéo dépasse la limite de %d Mo même compressée." % MAX_FILE_MB)
        return {
            "path": path, "folder": folder,
            "title": (info.get("title") or "Vidéo")[:200],
            "duration": int(duration) if duration else None,
        }
    except DownloadError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise BotError(friendly(exc)) from exc
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def cleanup(data):
    if data and data.get("folder"):
        shutil.rmtree(data["folder"], ignore_errors=True)


# ============================================================
# ENVOI AUDIO
# ============================================================

COVER_CACHE = {}
COVER_LOCK = threading.Lock()


def fetch_cover(tr, folder):
    if not tr.get("art"):
        return None
    url = tr["art"].replace("100x100bb", "300x300bb")
    if "=w" in url and "googleusercontent" in url:
        url = re.sub(r"=w\d+-h\d+.*$", "=w320-h320-l90-rj", url)      # pochette YouTube Music
    path = os.path.join(folder, "cover.jpg")
    try:
        with COVER_LOCK:
            blob = COVER_CACHE.get(url)
        if blob is None:                       # une seule requête par pochette (album = même image)
            with urlopen(Request(url, headers={"User-Agent": "MusicBotV4/4.0"}), timeout=8) as resp:
                blob = resp.read(250000)
            with COVER_LOCK:
                if len(COVER_CACHE) > 60:
                    COVER_CACHE.clear()
                COVER_CACHE[url] = blob
        if 0 < len(blob) < 200000:
            with open(path, "wb") as fh:
                fh.write(blob)
            return path
    except Exception:
        pass
    return None


def fetch_audio(tr):
    """Retourne soit {'file_id': ...} (cache), soit les données d'un fichier téléchargé."""
    fid = cache_get(tr["id"])
    if fid:
        return {"file_id": fid}
    return download_fresh(tr)


def download_fresh(tr):
    data = None
    if tr.get("vid"):                      # titre trouvé via YouTube Music : on connaît déjà la bonne vidéo
        try:
            data = _download_url("https://www.youtube.com/watch?v=%s" % tr["vid"],
                                 "%s %s" % (tr["artist"], tr["title"]), tr.get("dur"))
        except BotError as exc:
            log.warning("Vidéo YouTube Music inutilisable (%s), recherche classique : %s", tr["vid"], exc)
    if data is None:
        data = download_audio(query="%s %s" % (tr["artist"], tr["title"]), expected=tr.get("dur"),
                              artist=tr["artist"], title=tr["title"])
    data["cover"] = fetch_cover(tr, data["folder"])
    return data


def send_audio_file(chat_id, path, cover, common):
    with open(path, "rb") as audio:
        if cover and os.path.exists(cover):
            try:
                with open(cover, "rb") as thumb:
                    return bot.send_audio(chat_id, audio, thumb=thumb, **common)
            except Exception:
                log.warning("Envoi avec miniature échoué, nouvel essai sans.", exc_info=True)
                audio.seek(0)
        return bot.send_audio(chat_id, audio, **common)


def _done(chat_id, tr):
    """Historique + session d'écoute (ne doit jamais gêner l'envoi)."""
    try:
        extras.on_download(chat_id, tr)
    except Exception:
        log.warning("Historique non enregistré", exc_info=True)


def push_audio(chat_id, tr, data, caption, kb=None, silent=False):
    """Envoie l'audio puis supprime toujours le dossier temporaire."""
    common = {
        "caption": caption[:1000], "parse_mode": "HTML", "reply_markup": kb,
        "title": tr["title"][:200], "performer": tr["artist"][:100], "timeout": 180,
        "disable_notification": bool(silent),
    }
    try:
        if data.get("file_id"):
            try:
                bot.send_audio(chat_id, data["file_id"], **common)
                _done(chat_id, tr)
                return
            except Exception:
                log.warning("file_id périmé pour %s, retéléchargement.", tr["id"])
                cache_del(tr["id"])
                data = download_fresh(tr)

        path = data["path"]
        common["duration"] = data.get("duration") or tr.get("dur")
        if os.path.splitext(path)[1].lower() in (".m4a", ".mp3"):
            msg = send_audio_file(chat_id, path, data.get("cover"), common)
            if msg and msg.audio:
                cache_set(tr["id"], msg.audio.file_id)
        else:
            with open(path, "rb") as doc:
                bot.send_document(chat_id, doc, caption=caption[:1000], parse_mode="HTML", timeout=180,
                                  disable_notification=bool(silent))
        _done(chat_id, tr)
    finally:
        cleanup(data)


def track_caption(tr):
    text = "🎵 <b>%s</b>\n👤 %s" % (esc(tr["title"]), esc(tr["artist"]))
    if tr.get("album"):
        text += "\n💿 %s" % esc(tr["album"])
        if tr.get("year"):
            text += " (%s)" % esc(tr["year"])
    return text


def album_kb(tr):
    kb = types.InlineKeyboardMarkup()
    if (not tr.get("album_id") or str(tr.get("album") or "").endswith(" - Single")
            or (tr.get("album") and plain(tr["album"]) == plain(tr["title"]))):
        # Un single n'a qu'une piste : on propose plutôt la liste des albums de l'artiste.
        kb.add(types.InlineKeyboardButton("💿 Albums de l'artiste", callback_data="sa"))
        return kb
    kb.add(types.InlineKeyboardButton("💿 Télécharger l'album", callback_data="ab:%s" % tr["album_id"]))
    return kb


# ============================================================
# TÂCHES
# ============================================================

def track_kb(tr, vid, more=False):
    kb = album_kb(tr) or types.InlineKeyboardMarkup()
    if vid and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        kb.row(types.InlineKeyboardButton("🎲 Dans le même style", callback_data="sim:%s" % vid),
               types.InlineKeyboardButton("📝 Paroles", callback_data="lyr:%s" % vid))
    kb.add(types.InlineKeyboardButton("❤️ Ajouter aux favoris", callback_data="fv:%s" % tr["id"]))
    if more:
        kb.add(types.InlineKeyboardButton("🔁 Autres résultats", callback_data="mo"))
    return kb if kb.keyboard else None


def bar(step, total=4):
    return "▰" * step + "▱" * (total - step)


def job_track(chat_id, track_id, more=False):
    status = None
    try:
        status = send(chat_id, "⏳ <i>Préparation…</i>\n%s" % bar(1))
        tr = lookup_track(track_id)
        if not tr:
            raise BotError("Ce choix a expiré, renvoie ta recherche.")
        finder.remember(tr)                      # pour ❤️ et le partage inline
        label = "<b>%s</b> — %s" % (esc(tr["title"]), esc(tr["artist"]))
        cached = bool(cache_get(tr["id"]))
        edit(chat_id, status.message_id,
             ("⚡ %s\n%s Déjà en mémoire, envoi instantané" % (label, bar(3))) if cached
             else ("🔎 %s\n%s Recherche de la version officielle…" % (label, bar(2))))
        data = fetch_audio(tr)
        if not cached:
            edit(chat_id, status.message_id, "📤 %s\n%s Envoi…" % (label, bar(3)))
        vid = tr.get("vid") or (str(data.get("vid") or "").split("_", 1)[1] if str(data.get("vid") or "").startswith("youtube_") else None)
        push_audio(chat_id, tr, data, track_caption(tr), track_kb(tr, vid, more))
        safe_delete(chat_id, status.message_id)
    except Exception as exc:
        report(chat_id, status, exc)


def job_youtube(chat_id, query):
    status = None
    data = None
    try:
        status = send(chat_id, "⏳ <i>Recherche directe de <b>%s</b>…</i>" % esc(query))
        data = download_audio(query=query)
        tr = {"id": data["vid"], "title": data["title"], "artist": data["artist"], "dur": data.get("duration")}
        push_audio(chat_id, tr, data, "🎵 <b>%s</b>\n👤 %s" % (esc(tr["title"]), esc(tr["artist"])))
        safe_delete(chat_id, status.message_id)
    except Exception as exc:
        cleanup(data)
        report(chat_id, status, exc)


def job_youtube_url(chat_id, url):
    status = None
    data = None
    try:
        status = send(chat_id, "🎵 <i>Je récupère l'audio…</i>")
        data = download_audio(url=resolve_url(url))
        tr = {"id": data["vid"], "title": data["title"], "artist": data["artist"], "dur": data.get("duration")}
        push_audio(chat_id, tr, data, "🎵 <b>%s</b>\n👤 %s" % (esc(tr["title"]), esc(tr["artist"])))
        safe_delete(chat_id, status.message_id)
    except Exception as exc:
        cleanup(data)
        report(chat_id, status, exc)


def job_video(chat_id, url):
    status = None
    data = None
    try:
        status = send(chat_id, "🎬 <i>Je télécharge la vidéo…</i>")
        data = download_video(resolve_url(url), note=lambda t: edit(chat_id, status.message_id, "<i>%s</i>" % t))
        edit(chat_id, status.message_id, "📤 <i>Envoi de la vidéo…</i>")
        caption = ("🎬 <b>%s</b>" % esc(data["title"]))[:1000]
        try:
            with open(data["path"], "rb") as video:
                bot.send_video(chat_id, video, caption=caption, duration=data.get("duration"),
                               supports_streaming=True, parse_mode="HTML", timeout=300)
        except Exception:
            log.warning("send_video refusé, envoi en document.", exc_info=True)
            with open(data["path"], "rb") as doc:
                bot.send_document(chat_id, doc, caption=caption, parse_mode="HTML", timeout=300)
        safe_delete(chat_id, status.message_id)
    except Exception as exc:
        report(chat_id, status, exc)
    finally:
        cleanup(data)


def safe_fetch(tr):
    try:
        return fetch_audio(tr), None
    except Exception as exc:
        log.warning("Piste en échec : %s (%s)", tr.get("title"), exc)
        return None, exc


def human_time(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d s" % seconds
    return "%d min %02d s" % divmod(seconds, 60)


def job_album(chat_id, album_id):
    status = None
    try:
        status = send(chat_id, "💿 <i>Je récupère la liste des pistes…</i>")
        try:
            if str(album_id).startswith("yb:"):
                album, tracks = finder.album_tracks(str(album_id)[3:])
            else:
                album, tracks = album_tracks(album_id)
        except Exception:
            log.exception("Album introuvable : %s", album_id)
            raise BotError("Impossible de récupérer cet album pour le moment.")
        if not tracks:
            raise BotError("Cet album ne contient aucune piste exploitable.")
        name = album.get("collectionName") or "Album"
        run_tracks(chat_id, status, name, tracks, album.get("artist"))
    except Exception as exc:
        report(chat_id, None, exc)           # nouveau message : l'utilisateur est averti de l'erreur
        if status:
            safe_delete(chat_id, status.message_id)


def run_tracks(chat_id, status, name, tracks, artist=None):
    """Télécharge et envoie les pistes en pipeline (préparation d'avance), puis notifie à la fin."""
    futures = {}
    total_all = len(tracks)
    tracks = tracks[:MAX_ALBUM_TRACKS]
    total = len(tracks)
    started = time.time()
    ahead = ALBUM_AHEAD

    def prefetch(i):
        if i < total and i not in futures:
            futures[i] = album_pool.submit(safe_fetch, tracks[i])

    def progress(done, current):
        eta = ""
        if done:
            eta = "\n⏱ Environ %s restantes" % human_time((time.time() - started) / done * (total - done))
        edit(chat_id, status.message_id,
             "💿 <b>%s</b>\n%s  <b>%d/%d</b>\n⏬ %s%s\n\n"
             "<i>🔔 Tu peux faire autre chose : je t'envoie un message dès que tout est prêt.</i>"
             % (esc(name), bar(done, total), done, total, esc(current), eta))

    try:
        for i in range(min(ahead, total)):
            prefetch(i)
        ok, failed, reasons, bad = 0, [], [], []
        for i, tr in enumerate(tracks):
            prefetch(i + ahead)
            progress(i, tr["title"])
            try:
                bot.send_chat_action(chat_id, "upload_audio")
            except Exception:
                pass
            try:
                data, err = futures.pop(i).result(timeout=ALBUM_TRACK_TIMEOUT)
            except Exception as exc:                      # délai dépassé : on passe à la piste suivante
                data, err = None, exc if str(exc) else BotError("délai dépassé")
            if err:
                failed.append(tr["title"])
                bad.append(tr)
                reasons.append("%s : %s" % (tr["title"], (str(err) or type(err).__name__)[:120]))
                continue
            try:
                caption = "💿 <b>%d/%d</b> · %s\n👤 %s" % (i + 1, total, esc(tr["title"]), esc(tr["artist"]))
                push_audio(chat_id, tr, data, caption, silent=True)      # silencieux : un seul « ding » à la fin
                ok += 1
            except Exception:
                log.exception("Envoi piste échoué : %s", tr["title"])
                failed.append(tr["title"])
                bad.append(tr)
                reasons.append("%s : envoi refusé" % tr["title"])

        took = human_time(time.time() - started)
        head = "🎉 <b>Terminé !</b>" if not failed else "⚠️ <b>Terminé avec des échecs</b>"
        text = "%s\n💿 <b>%s</b>%s\n✅ %d/%d pistes envoyées en %s" % (
            head, esc(name), (" — %s" % esc(artist)) if artist else "", ok, total, took)
        if total_all > total:
            text += "\n<i>Limité à %d pistes sur %d.</i>" % (total, total_all)
        kb = None
        if failed:
            text += "\n\n❌ Échec : " + esc(", ".join(failed[:5]))
            if reasons:
                text += "\n<i>Cause : %s</i>" % esc(reasons[0][:200])
            set_state(chat_id, retry=(name, bad))
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🔁 Réessayer les pistes en échec", callback_data="rt"))
        safe_delete(chat_id, status.message_id)
        send_fx(chat_id, text, None if failed else EFFECT_PARTY, reply_markup=kb)   # nouveau message => notification + confettis
    finally:
        for fut in futures.values():
            try:
                fut.cancel()
                if fut.done() and not fut.cancelled():
                    cleanup((fut.result() or (None, None))[0])
            except Exception:
                pass


def song_buttons(songs, limit=8):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for tr in songs[:limit]:
        mins = ("  · %d:%02d" % divmod(int(tr["dur"]), 60)) if tr.get("dur") else ""
        kb.add(types.InlineKeyboardButton(("%s — %s%s" % (tr["title"], tr["artist"], mins))[:60],
                                          callback_data="tr:%s" % tr["id"]))
    return kb


def job_similar(chat_id, vid):
    try:
        songs = finder.related(vid)
        if not songs:
            return send(chat_id, "🤷 Je n'ai pas trouvé de titres dans le même style pour celui-ci.")
        set_state(chat_id, songs=songs[:6])
        send(chat_id, "🎲 <b>Dans le même style</b>\n<i>Touche un titre pour le recevoir.</i>",
             reply_markup=song_buttons(songs))
    except Exception as exc:
        report(chat_id, None, exc)


def job_lyrics(chat_id, vid):
    try:
        text = finder.lyrics(vid)
        if not text:
            return send(chat_id, "📝 Pas de paroles disponibles pour ce titre.")
        for k in range(0, min(len(text), 7000), 3500):
            send(chat_id, ("📝 <b>Paroles</b>\n\n" if k == 0 else "") + esc(text[k:k + 3500]))
    except Exception as exc:
        report(chat_id, None, exc)


def job_trending(chat_id):
    try:
        songs = finder.trending(TREND_COUNTRY)
        if not songs:
            return send(chat_id, "🔥 Le classement n'est pas disponible pour le moment.")
        set_state(chat_id, songs=songs[:6])
        send(chat_id, "🔥 <b>Tendances du moment</b> (%s)\n<i>Touche un titre pour le recevoir.</i>" % esc(TREND_COUNTRY),
             reply_markup=song_buttons(songs, 12))
    except Exception as exc:
        report(chat_id, None, exc)


def job_favorites(chat_id):
    try:
        favs = extras.fav_list(chat_id)
        if not favs:
            return send(chat_id, "❤️ Pas encore de favoris.\n<i>Touche « ❤️ Ajouter aux favoris » sous un titre.</i>")
        set_state(chat_id, songs=favs[:6])
        send(chat_id, "❤️ <b>Tes favoris</b> (%d)\n<i>Touche un titre pour le recevoir.</i>" % len(favs),
             reply_markup=song_buttons(favs, 20))
    except Exception as exc:
        report(chat_id, None, exc)


MEDALS = ["🥇", "🥈", "🥉"]


def wrapped_text(chat_id, days):
    st = extras.stats_for(chat_id, days)
    label = "tout ton historique" if not days else "les %d derniers jours" % days
    if not st["total"]:
        return "📊 <b>Ton Wrapped</b> · %s\n\nPas encore d'écoute sur cette période. Lance-toi 🎧" % label
    lines = ["📊 <b>Ton Wrapped</b> · %s" % label, "", "🎧 <b>%d</b> titre(s) écouté(s)" % st["total"], ""]
    if st["artists"]:
        lines.append("🏆 <b>Tes artistes</b>")
        for i, (name, n) in enumerate(st["artists"]):
            lines.append("%s %s · %d" % (MEDALS[i], esc(name), n))
        lines.append("")
    if st["titles"]:
        lines.append("🔥 <b>Tes titres phares</b>")
        for i, (title, artist, n) in enumerate(st["titles"], 1):
            lines.append("%d. %s — %s%s" % (i, esc(title), esc(artist), (" ×%d" % n) if n > 1 else ""))
    return "\n".join(lines)


def wrapped_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("7 jours", callback_data="wr:7"),
           types.InlineKeyboardButton("30 jours", callback_data="wr:30"),
           types.InlineKeyboardButton("Tout", callback_data="wr:0"))
    return kb


def job_retry(chat_id):
    status = None
    try:
        name, bad = get_state(chat_id).get("retry") or (None, [])
        if not bad:
            raise BotError("Rien à réessayer.")
        status = send(chat_id, "🔁 <i>Je réessaie %d piste(s)…</i>" % len(bad))
        run_tracks(chat_id, status, name or "Album", bad)
    except Exception as exc:
        report(chat_id, status, exc)


# ============================================================
# RECHERCHE
# ============================================================

def job_artist(chat_id, artist_id, hint="", status_id=None):
    """Fiche artiste : photo (pochette du plus gros titre), top titres et albums en boutons."""
    try:
        name, genre, songs = artist_top(artist_id)
        name = name or hint
        if not songs:
            raise BotError("Je n'ai trouvé aucun titre pour cet artiste.")
        set_state(chat_id, artist=name, artist_id=str(artist_id))
        kb = types.InlineKeyboardMarkup(row_width=1)
        for i, tr in enumerate(songs[:5], 1):
            kb.add(types.InlineKeyboardButton(("%d. %s" % (i, tr["title"]))[:60], callback_data="tr:%s" % tr["id"]))
        row = [types.InlineKeyboardButton("💿 Albums", callback_data="sa")]
        if get_state(chat_id).get("cands"):
            row.append(types.InlineKeyboardButton("👥 Pas le bon ?", callback_data="ao"))
        kb.row(*row)
        caption = "👤 <b>%s</b>" % esc(name)
        if genre:
            caption += "\n🎼 %s" % esc(genre)
        caption += "\n🔥 <i>Ses titres les plus écoutés : touche pour télécharger.</i>"
        if status_id:
            safe_delete(chat_id, status_id)
        art = songs[0].get("art")
        if art:
            try:
                bot.send_photo(chat_id, art.replace("100x100bb", "600x600bb"), caption=caption,
                               parse_mode="HTML", reply_markup=kb)
                return
            except Exception:
                log.warning("Photo de la fiche artiste refusée, envoi en texte.", exc_info=True)
        send(chat_id, caption, reply_markup=kb)
    except Exception as exc:
        report(chat_id, None, exc)


def show_results(chat_id, query):
    status = send(chat_id, "🔎 <i>Je cherche <b>%s</b>…</i>" % esc(query))
    set_state(chat_id, query=query, artist=None, artist_id=None, cands=[])
    try:
        res = smart_search(query)
    except Exception:
        log.exception("Recherche iTunes échouée")
        res = None

    if res and res["mode"] == "card":
        set_state(chat_id, cands=res["others"])
        return job_artist(chat_id, res["artist"]["id"], res["artist"]["name"], status.message_id)

    if res and res["mode"] == "auto":
        tr = res["track"]
        set_state(chat_id, artist=tr["artist"], artist_id=tr["artist_id"], cands=res["others"],
                  songs=res.get("songs") or [])
        edit(chat_id, status.message_id, "✅ Trouvé : <b>%s</b> — %s" % (esc(tr["title"]), esc(tr["artist"])))
        if not submit(chat_id, job_track, chat_id, tr["id"], True):
            edit(chat_id, status.message_id, "Déjà %d tâches en cours, patiente un instant." % MAX_PENDING)
        return

    it_songs = res["songs"] if res else []
    it_best_score, it_best = 0.0, None
    for t in it_songs:
        sc = finder.score(query, t["artist"], t["title"])
        if sc > it_best_score:
            it_best_score, it_best = sc, t

    # --- YouTube Music : artistes peu connus, titres avec fautes ---
    ranked, artist_info = [], None
    try:
        if len(plain(query).split()) <= 2:
            artist_info = finder.artist_page(query)
        if not artist_info:
            ranked = finder.find(query, MAX_DURATION)
    except Exception:
        log.exception("Recherche YouTube Music échouée")

    if artist_info:
        name, art, songs = artist_info
        return show_artist_ytm(chat_id, status.message_id, name, art, songs)

    ytm_best = ranked[0][0] if ranked else 0.0
    verdict = finder.decide(ranked) if ranked else "none"
    log.info("Recherche %r : iTunes %.2f | YouTube Music %.2f (%s)", query, it_best_score, ytm_best, verdict)

    if it_best and it_best_score >= finder.AUTO_MIN and it_best_score > ytm_best + 0.05:
        known = res["artist"]
        songs = sorted(it_songs, key=lambda t: -finder.score(query, t["artist"], t["title"]))
        set_state(chat_id, artist=(known or {}).get("name") or it_best["artist"],
                  artist_id=(known or {}).get("id") or it_best["artist_id"],
                  cands=res["others"], songs=songs[:6])
        return start_track(chat_id, status.message_id, it_best)

    if verdict == "auto":
        tr = ranked[0][1]
        set_state(chat_id, artist=tr["artist"], artist_id=None, songs=[t for _, t in ranked[:6]])
        return start_track(chat_id, status.message_id, tr)

    if ranked:
        set_state(chat_id, songs=[t for _, t in ranked[:6]])
        kb = types.InlineKeyboardMarkup(row_width=1)
        for n, (_, tr) in enumerate(ranked[:5]):
            star = "⭐ " if n == 0 else ""
            mins = ("  · %d:%02d" % divmod(int(tr["dur"]), 60)) if tr.get("dur") else ""
            kb.add(types.InlineKeyboardButton(("%s%s — %s%s" % (star, tr["title"], tr["artist"], mins))[:60],
                                              callback_data="tr:%s" % tr["id"]))
        kb.add(types.InlineKeyboardButton("▶️ Recherche directe", callback_data="yt"))
        edit(chat_id, status.message_id,
             "🤔 Je ne suis pas sûr à 100 %% pour <b>%s</b>.\n<i>C'est plutôt l'un de ceux-ci ?</i>" % esc(query),
             reply_markup=kb)
        return

    # Rien de crédible dans les catalogues : recherche directe YouTube.
    edit(chat_id, status.message_id, "▶️ Je cherche directement <b>%s</b>…" % esc(query))
    if not submit(chat_id, job_youtube, chat_id, query):
        edit(chat_id, status.message_id, "Déjà %d tâches en cours, patiente un instant." % MAX_PENDING)


def show_album_search(chat_id, term):
    """« album <nom> » : cherche l'album (fautes tolérées), YouTube Music puis iTunes."""
    status = send(chat_id, "💿 <i>Je cherche l'album <b>%s</b>…</i>" % esc(term))
    kb = types.InlineKeyboardMarkup(row_width=1)
    shown = 0
    for al in finder.search_albums(term)[:6]:
        icon = "💿" if al["type"] == "Album" else "📀" if al["type"] == "EP" else "🎵"
        label = ("%s %s — %s · %s" % (icon, al["name"], al["artist"], al["year"])).strip(" ·")[:60]
        kb.add(types.InlineKeyboardButton(label, callback_data="ab:yb:%s" % al["id"]))
        shown += 1
    if not shown:
        try:
            tags = {0: "💿", 1: "📀 EP", 2: "🎵 Single"}
            for al in search_albums(term)[:6]:
                label = ("%s %s · %s" % (tags[al["kind"]], al["name"], al["year"])).strip(" ·")[:60]
                kb.add(types.InlineKeyboardButton(label, callback_data="ab:%s" % al["id"]))
                shown += 1
        except Exception:
            log.exception("Recherche d'albums iTunes échouée")
    if not shown:
        return edit(chat_id, status.message_id, "⚠️ Aucun album trouvé pour <b>%s</b>." % esc(term))
    edit(chat_id, status.message_id, "💿 <b>Albums trouvés</b>\n<i>Touche celui que tu veux.</i>", reply_markup=kb)


def start_track(chat_id, status_id, tr):
    edit(chat_id, status_id, "✅ Trouvé : <b>%s</b> — %s" % (esc(tr["title"]), esc(tr["artist"])))
    if not submit(chat_id, job_track, chat_id, tr["id"], True):
        edit(chat_id, status_id, "Déjà %d tâches en cours, patiente un instant." % MAX_PENDING)


def show_artist_ytm(chat_id, status_id, name, art, songs):
    """Fiche d'un artiste trouvé sur YouTube Music (même peu connu)."""
    set_state(chat_id, artist=name, artist_id=None, songs=songs, cands=[])
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, tr in enumerate(songs[:5], 1):
        kb.add(types.InlineKeyboardButton(("%d. %s" % (i, tr["title"]))[:60], callback_data="tr:%s" % tr["id"]))
    kb.add(types.InlineKeyboardButton("💿 Albums", callback_data="sa"))
    caption = "👤 <b>%s</b>\n🔥 <i>Ses titres : touche pour télécharger.</i>" % esc(name)
    safe_delete(chat_id, status_id)
    if art:
        try:
            bot.send_photo(chat_id, art, caption=caption, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            log.warning("Photo d'artiste refusée, envoi en texte.", exc_info=True)
    send(chat_id, caption, reply_markup=kb)


# ============================================================
# HANDLERS
# ============================================================

def send_menu(chat_id, name):
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🎵 Musique", callback_data="mn:music"),
           types.InlineKeyboardButton("🎬 Lien / Vidéo", callback_data="mn:link"))
    kb.row(types.InlineKeyboardButton("🔥 Tendances", callback_data="mn:trend"),
           types.InlineKeyboardButton("❓ Aide", callback_data="mn:help"))
    kb.row(types.InlineKeyboardButton("❤️ Favoris", callback_data="mn:fav"),
           types.InlineKeyboardButton("📊 Wrapped", callback_data="mn:wrapped"))
    kb.row(types.InlineKeyboardButton("⭐ Mon avis", callback_data="av:start"))
    send(chat_id,
         "🎧 <b>Salut %s !</b> Je suis <b>Téo</b>.\n\n"
         "🎵 Écris un <b>artiste</b>, un <b>titre</b> ou <b>artiste titre</b>.\n"
         "🔗 Colle un <b>lien</b> pour l'audio ou la vidéo.\n"
         "💿 Je peux aussi t'envoyer des albums complets." % esc(name), reply_markup=kb)


def is_busy(chat_id):
    with pending_lock:
        return pending.get(chat_id, 0) > 0


# Doit être appelé AVANT les handlers ci-dessous : la porte d'entrée (code d'accès) passe en premier.
extras.install(bot, esc=esc, menu=send_menu, busy=is_busy)


HELP_TEXT = ("🎧 <b>Aide</b>\n\n"
             "• Un artiste (« mhd ») → sa fiche et ses meilleurs titres\n"
             "• « mhd afro trap » ou « artiste titre » → le bon titre direct\n"
             "• 💿 Albums → liste des albums · ou écris « album <nom> »\n"
             "• 🔥 /tendances → ce qui cartonne en ce moment\n"
             "• ❤️ /favoris · 📊 /wrapped → tes préférés et ton résumé d'écoute\n"
             "• Dans n'importe quelle discussion, tape @nom_du_bot + un titre pour le partager\n"
             "• Sous chaque titre : 🎲 même style · 📝 paroles\n"
             "• Lien → audio 🎵 ou vidéo 🎬\n"
             "• Plusieurs demandes d'affilée : elles passent en file\n"
             "• /avis pour donner ton avis · /stop pour couper les suggestions")


@bot.message_handler(commands=["start"])
def cmd_start(message):
    clear_state(message.chat.id)
    user = message.from_user
    name = user.first_name if user else "ami"
    uid = user.id if user else message.chat.id
    extras.touch(uid, name)
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if payload.startswith("ym_") and re.fullmatch(r"[A-Za-z0-9_-]{11}", payload[3:]):
        if extras.is_ok(uid):                      # lien « Télécharger avec Téo » venu du mode inline
            return submit(message.chat.id, job_track, message.chat.id, "ym:" + payload[3:])
    if extras.is_owner(uid):
        extras.owner_hello(message.chat.id, name)
    send_menu(message.chat.id, name)


_BOT_USER = {}


def bot_username():
    if "u" not in _BOT_USER:
        try:
            _BOT_USER["u"] = bot.get_me().username
        except Exception:
            return None
    return _BOT_USER["u"]


@bot.inline_handler(lambda q: True)
def on_inline(iq):
    """@Téo <titre> dans n'importe quelle discussion : partage instantané des titres déjà en mémoire."""
    try:
        uid = iq.from_user.id
        results = []
        if not extras.is_ok(uid):
            results.append(types.InlineQueryResultArticle(
                id="locked", title="🔒 Bot privé", description="Ouvre d'abord le bot pour entrer ton code.",
                input_message_content=types.InputTextMessageContent("🔒 Téo est un bot privé.")))
            return bot.answer_inline_query(iq.id, results, cache_time=5, is_personal=True)
        text = (iq.query or "").strip()
        if len(text) < 2:
            songs = extras.fav_list(uid, 8)
            note = "Tes favoris" if songs else "Écris un titre ou un artiste…"
        else:
            songs = [t for _, t in finder.find(text, MAX_DURATION)[:6]]
            note = ""
        user = bot_username()
        for tr in songs:
            fid = cache_get(tr["id"])
            rid = (tr.get("vid") or str(tr["id"]))[:60]
            if fid:                                   # déjà en mémoire : le titre part tout de suite
                results.append(types.InlineQueryResultCachedAudio(id=rid, audio_file_id=fid))
                continue
            mins = ("%d:%02d" % divmod(int(tr["dur"]), 60)) if tr.get("dur") else ""
            kb = None
            if user and tr.get("vid"):
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("⬇️ Télécharger avec Téo",
                                                  url="https://t.me/%s?start=ym_%s" % (user, tr["vid"])))
            results.append(types.InlineQueryResultArticle(
                id=rid, title=tr["title"][:100], description=("%s · %s" % (tr["artist"], mins)).strip(" ·"),
                input_message_content=types.InputTextMessageContent(
                    "🎵 <b>%s</b>\n👤 %s" % (esc(tr["title"]), esc(tr["artist"])), parse_mode="HTML"),
                reply_markup=kb))
        if not results:
            results.append(types.InlineQueryResultArticle(
                id="none", title=note or "Aucun résultat", description="Essaie un autre titre.",
                input_message_content=types.InputTextMessageContent("🎵 Téo")))
        bot.answer_inline_query(iq.id, results, cache_time=20, is_personal=True)
    except Exception:
        log.exception("Mode inline en échec")


@bot.message_handler(commands=["tendances", "top"])
def cmd_trending(message):
    if submit(message.chat.id, job_trending, message.chat.id):
        send(message.chat.id, "🔥 <i>Je regarde ce qui cartonne…</i>")


@bot.message_handler(commands=["favoris", "fav"])
def cmd_favs(message):
    submit(message.chat.id, job_favorites, message.chat.id)


@bot.message_handler(commands=["wrapped", "semaine"])
def cmd_wrapped(message):
    send(message.chat.id, wrapped_text(message.chat.id, 7), reply_markup=wrapped_kb())


@bot.message_handler(commands=["aide", "help"])
def cmd_help(message):
    send(message.chat.id, HELP_TEXT)


def _mem_info():
    try:
        def rd(path):
            with open(path) as fh:
                return fh.read().strip()
        cur = int(rd("/sys/fs/cgroup/memory.current")) // 2 ** 20
        lim = rd("/sys/fs/cgroup/memory.max")
        return "%d Mo / %s" % (cur, ("%d Mo" % (int(lim) // 2 ** 20)) if lim.isdigit() else "illimité")
    except Exception:
        return "inconnue"


def _has_ejs():
    try:
        import yt_dlp_ejs  # noqa: F401
        return True
    except Exception:
        return False


@bot.message_handler(commands=["diag"])
def cmd_diag(message):
    import yt_dlp
    lines = ["🩺 <b>Diagnostic</b>",
             "yt-dlp : %s" % yt_dlp.version.__version__,
             "ffmpeg : %s · ffprobe : %s" % ("oui" if FFMPEG else "non", "oui" if shutil.which("ffprobe") else "non"),
             "Cookies : %s" % ("oui" if COOKIE_SRC else "non"),
             "YouTube Music : %s" % ("oui" if finder.available() else "NON (ytmusicapi absent)"),
             "Moteur JS (deno) : %s" % ("oui" if shutil.which("deno") else "NON — requis pour YouTube"),
             "yt-dlp-ejs : %s" % ("oui" if _has_ejs() else "NON"),
             "Mémoire : %s" % _mem_info(),
             "Cache : %d titres" % len(cache), "Version : mise à jour 5"]
    try:
        urls = rank_candidates("Niska Réseaux (Official Audio)", 190)
        lines.append("YouTube recherche : ✅ (%d candidats)" % len(urls))
    except Exception as exc:
        lines.append("YouTube recherche : ❌ %s" % esc(str(exc)[:200]))
    send(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["purgecache"])
def cmd_purge(message):
    uid = message.from_user.id if message.from_user else message.chat.id
    if not extras.is_owner(uid):
        return
    with cache_lock:
        cache.clear()
        _cache_save()
    send(message.chat.id, "🧹 Cache vidé.")


@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    try:
        chat_id = call.message.chat.id
    except Exception:
        return
    data = call.data or ""
    extras.touch(call.from_user.id, call.from_user.first_name)

    def queue(fn, *args, msg="Préparation…"):
        if submit(chat_id, fn, *args):
            answer(call, msg)
        else:
            answer(call, "Déjà %d tâches en cours, patiente un instant." % MAX_PENDING, alert=True)

    if data.startswith("mn:"):
        answer(call)
        what = data[3:]
        if what == "music":
            send(chat_id, "🎵 Écris un <b>artiste</b>, un <b>titre</b> ou <b>artiste titre</b>.")
        elif what == "fav":
            return queue(job_favorites, chat_id, msg="Tes favoris…")
        elif what == "wrapped":
            return send(chat_id, wrapped_text(chat_id, 7), reply_markup=wrapped_kb())
        elif what == "trend":
            return queue(job_trending, chat_id, msg="Classement…")
        elif what == "link":
            send(chat_id, "🔗 Colle ici un lien (YouTube, Instagram, TikTok…).")
        else:
            send(chat_id, HELP_TEXT)
        return

    if data.startswith("ar:"):
        aid = data[3:]
        if not aid.isdigit():
            return answer(call, "Artiste invalide.", alert=True)
        return queue(job_artist, chat_id, aid, msg="Ouverture de la fiche…")

    if data == "ao":
        cands = get_state(chat_id).get("cands") or []
        if not cands:
            return answer(call, "Pas d'autre artiste proposé.", alert=True)
        answer(call)
        kb = types.InlineKeyboardMarkup(row_width=1)
        for a in cands[:5]:
            label = ("%s · %s" % (a["name"], a["genre"]) if a.get("genre") else a["name"])[:60]
            kb.add(types.InlineKeyboardButton(label, callback_data="ar:%s" % a["id"]))
        send(chat_id, "👥 <b>C'est plutôt l'un d'eux ?</b>", reply_markup=kb)
        return

    if data.startswith("tr:"):
        tid = data[3:]
        if not (tid.isdigit() or re.fullmatch(r"ym:[A-Za-z0-9_-]{6,15}", tid)):
            return answer(call, "Titre invalide.", alert=True)
        return queue(job_track, chat_id, tid)

    if data.startswith("ab:"):
        aid = data[3:]
        if not (aid.isdigit() or re.fullmatch(r"yb:[A-Za-z0-9_-]{8,40}", aid)):
            return answer(call, "Album invalide.", alert=True)
        return queue(job_album, chat_id, aid, msg="Album lancé…")

    if data == "sa":
        st = get_state(chat_id)
        term = st.get("artist") or st.get("query")
        if not term:
            return answer(call, "La recherche a expiré, renvoie ton titre.", alert=True)
        answer(call, "Recherche des albums…")
        kb = types.InlineKeyboardMarkup(row_width=1)
        shown = 0
        try:
            albums = artist_albums(st["artist_id"]) if st.get("artist_id") else []
        except Exception:
            albums = []
        if albums:
            tags = {0: "💿", 1: "📀 EP", 2: "🎵 Single"}
            for al in albums:
                label = "%s %s · %s" % (tags[al["kind"]], al["name"][:26], al["year"])
                kb.add(types.InlineKeyboardButton(label.strip(" ·"), callback_data="ab:%s" % al["id"]))
                shown += 1
        else:
            for al in finder.search_albums(term):
                if finder._sim(plain(al["artist"].split(", ")[0]), plain(term)) < 0.8 and plain(term) not in plain(al["artist"]):
                    continue
                label = "%s %s · %s" % ("💿" if al["type"] == "Album" else "📀" if al["type"] == "EP" else "🎵",
                                        al["name"][:26], al["year"])
                kb.add(types.InlineKeyboardButton(label.strip(" ·"), callback_data="ab:yb:%s" % al["id"]))
                shown += 1
            if not shown:
                try:
                    for al in search_albums(term):
                        tags = {0: "💿", 1: "📀 EP", 2: "🎵 Single"}
                        label = "%s %s · %s" % (tags[al["kind"]], al["name"][:26], al["year"])
                        kb.add(types.InlineKeyboardButton(label.strip(" ·"), callback_data="ab:%s" % al["id"]))
                        shown += 1
                except Exception:
                    return answer(call, "Catalogue indisponible.", alert=True)
        if not shown:
            return send(chat_id, "Aucun album trouvé pour <b>%s</b>." % esc(term))
        send(chat_id,
             "💿 Albums de <b>%s</b>\n<i>Touche-en un pour le recevoir.</i>" % esc(term), reply_markup=kb)
        return

    if data.startswith(("sim:", "lyr:")):
        vid = data[4:]
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return answer(call, "Titre invalide.", alert=True)
        return queue(job_similar if data.startswith("sim:") else job_lyrics, chat_id, vid,
                     msg="Je cherche…" if data.startswith("sim:") else "Je cherche les paroles…")

    if data.startswith("fv:"):
        tid = data[3:]
        tr = lookup_track(tid) if (tid.isdigit() or tid.startswith("ym:")) else None
        if not tr and tid.startswith("ym:"):
            au = getattr(call.message, "audio", None)           # vieux message après redémarrage : on lit le fichier audio
            if au is not None and getattr(au, "title", None):
                tr = {"id": tid, "vid": tid[3:], "title": au.title, "artist": getattr(au, "performer", None) or "",
                      "dur": getattr(au, "duration", None), "art": None, "album": "", "album_id": None, "artist_id": None}
        if not tr:
            return answer(call, "Titre introuvable, relance ta recherche.", alert=True)
        added = extras.fav_toggle(chat_id, tr)
        return answer(call, "❤️ Ajouté à tes favoris" if added else "💔 Retiré de tes favoris")

    if data.startswith("wr:"):
        days = data[3:]
        if days not in ("7", "30", "0"):
            return answer(call, "Période invalide.", alert=True)
        answer(call)
        edit(chat_id, call.message.message_id, wrapped_text(chat_id, int(days) or None), reply_markup=wrapped_kb())
        return

    if data == "rt":
        if not (get_state(chat_id).get("retry") or (None, []))[1]:
            return answer(call, "Rien à réessayer.", alert=True)
        return queue(job_retry, chat_id, msg="Nouvel essai…")

    if data == "mo":
        songs = get_state(chat_id).get("songs") or []
        if not songs:
            return answer(call, "Lance une nouvelle recherche.", alert=True)
        answer(call)
        kb = types.InlineKeyboardMarkup(row_width=1)
        for tr in songs:
            kb.add(types.InlineKeyboardButton(("%s — %s" % (tr["title"], tr["artist"]))[:60],
                                              callback_data="tr:%s" % tr["id"]))
        kb.add(types.InlineKeyboardButton("▶️ Recherche directe", callback_data="yt"))
        send(chat_id, "🔁 <b>Autres résultats</b>", reply_markup=kb)
        return

    if data == "yt":
        query = get_state(chat_id).get("query")
        if not query:
            return answer(call, "La recherche a expiré, renvoie ton titre.", alert=True)
        return queue(job_youtube, chat_id, query)

    if data in ("dv", "da"):
        url = get_state(chat_id).get("url")
        if not url:
            return answer(call, "Le lien a expiré, renvoie-le.", alert=True)
        return queue(job_video if data == "dv" else job_youtube_url, chat_id, url)

    answer(call)


@bot.message_handler(content_types=["text"])
def on_text(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    extras.touch(message.from_user.id if message.from_user else chat_id,
                 message.from_user.first_name if message.from_user else None)

    if URL_RE.match(text):
        set_state(chat_id, url=text[:2000])
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🎵 Audio", callback_data="da"),
            types.InlineKeyboardButton("🎬 Vidéo", callback_data="dv"),
        )
        send(chat_id, "🔗 Que veux-tu récupérer ?", reply_markup=kb)
        return

    m = re.match(r"^albums?\s+(.{2,})$", text, re.I)
    if m:
        return show_album_search(chat_id, m.group(1).strip()[:150])

    show_results(chat_id, text[:200])


# ============================================================
# MAIN
# ============================================================

def cache_json():
    with cache_lock:
        return json.dumps(cache)


def main():
    setup_cookies()
    try:
        backup.restore(bot, extras.DB_FILE, CACHE_FILE)        # disque neuf (redéploiement) : on récupère tout
    except Exception:
        log.exception("Restauration ignorée")
    cache_load()
    extras.start()
    backup.start(bot, cache_json)
    threading.Thread(target=start_web, daemon=True, name="web").start()
    threading.Thread(target=keep_alive, daemon=True, name="keepalive").start()

    try:
        bot.remove_webhook()
    except Exception:
        log.exception("remove_webhook a échoué")
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Ouvrir le menu"),
            types.BotCommand("tendances", "Les titres du moment"),
            types.BotCommand("favoris", "Mes titres préférés"),
            types.BotCommand("wrapped", "Mon résumé d'écoute"),
            types.BotCommand("aide", "Afficher l'aide"),
            types.BotCommand("avis", "Donner mon avis"),
            types.BotCommand("stop", "Couper les suggestions"),
        ])
    except Exception:
        log.exception("set_my_commands a échoué")

    log.info("MusicBot V4 démarré (ffmpeg=%s).", "oui" if FFMPEG else "non")

    while True:
        try:
            bot.infinity_polling(
                timeout=20, long_polling_timeout=20, skip_pending=True,
                allowed_updates=["message", "callback_query", "inline_query"],
            )
        except Exception as exc:
            log.warning("Coupure détectée (%s), reprise dans 5 s…", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
