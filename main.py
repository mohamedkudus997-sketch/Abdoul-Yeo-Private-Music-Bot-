# -*- coding: utf-8 -*-
"""
MusicBot V3 — Telegram + Render
- Résultats cliquables (iTunes) -> un seul tap pour télécharger
- Cache file_id : un titre déjà envoyé repart instantanément
- File d'attente par utilisateur (plus de blocage après 2-3 demandes)
- Albums complets (téléchargement en avance, envoi dans l'ordre)
- Timeouts d'upload adaptés (cause fréquente d'échecs sur Render)
Variables Render : BOT_TOKEN (obligatoire), YT_COOKIES (optionnel)
"""

import html
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from tempfile import mkdtemp
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import telebot
from flask import Flask
from telebot import apihelper, types
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

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
album_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="album")

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


def cache_get(key):
    with cache_lock:
        return cache.get(key)


def cache_set(key, file_id):
    with cache_lock:
        cache[key] = file_id
        _cache_save()


def cache_del(key):
    with cache_lock:
        cache.pop(key, None)
        _cache_save()


# ============================================================
# FLASK / RENDER
# ============================================================

@app.get("/")
def home():
    return "MusicBot V3 is running", 200


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
    msg = str(exc) if isinstance(exc, BotError) else "Je n'ai pas pu récupérer ce titre pour le moment."
    text = "⚠️ " + esc(msg)
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
    req = Request(url, headers={"User-Agent": "MusicBotV3/3.0"})
    last = None
    for _ in range(2):
        try:
            with urlopen(req, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise last


def norm(item):
    ms = item.get("trackTimeMillis")
    return {
        "id": str(item.get("trackId") or ""),
        "title": item.get("trackName") or "Titre inconnu",
        "artist": item.get("artistName") or "Artiste inconnu",
        "album": item.get("collectionName"),
        "album_id": str(item["collectionId"]) if item.get("collectionId") else None,
        "year": (item.get("releaseDate") or "")[:4],
        "dur": int(ms / 1000) if ms else None,
        "art": item.get("artworkUrl100"),
    }


def search_tracks(query):
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
        if len(out) >= 8:
            break
    return out


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


def ydl_base(folder):
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
    if COOKIE_SRC:
        copy = os.path.join(folder, "cookies.txt")   # copie par appel : pas de conflit entre threads
        try:
            shutil.copy(COOKIE_SRC, copy)
            opts["cookiefile"] = copy
        except Exception:
            pass
    return opts


def friendly(exc):
    text = str(exc).lower()
    if "sign in" in text or "not a bot" in text or "cookies" in text:
        return "YouTube bloque temporairement le serveur. Réessaie plus tard."
    if "unavailable" in text or "private" in text:
        return "Ce média n'est pas disponible."
    if "larger than max-filesize" in text or "max-filesize" in text:
        return "Le fichier dépasse la limite de %d Mo." % MAX_FILE_MB
    return "Le service n'a pas pu récupérer ce média pour le moment."


def find_media(folder, allowed):
    best, best_size = None, -1
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in allowed:
            size = os.path.getsize(path)
            if size > best_size:
                best, best_size = path, size
    return best


def pick_source(query, expected, prefix="ytsearch5"):
    """Une seule recherche (rapide), puis choix du résultat dont la durée colle le mieux."""
    folder = mkdtemp(prefix="srch_")
    try:
        opts = ydl_base(folder)
        opts.pop("max_filesize", None)
        opts["extract_flat"] = True
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info("%s:%s" % (prefix, query), download=False)
    except DownloadError as exc:
        log.warning("Recherche %s échouée : %s", prefix, exc)
        raise BotError(friendly(exc)) from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    is_yt = prefix.startswith("yt")
    entries = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        e["_url"] = e.get("url") or ("https://www.youtube.com/watch?v=%s" % e["id"] if is_yt else None)
        if e["_url"]:
            entries.append(e)
    if not entries:
        raise BotError("Aucun résultat trouvé pour ce titre.")
    ok = [e for e in entries if not e.get("duration") or e["duration"] <= MAX_DURATION]
    if not ok:
        raise BotError("Les résultats dépassent la limite de %d min." % (MAX_DURATION // 60))
    if expected:
        best = min(ok, key=lambda e: abs(e["duration"] - expected) if e.get("duration") else 600)
        if not is_yt and best.get("duration") and abs(best["duration"] - expected) > 60:
            raise BotError("Aucun résultat fiable trouvé pour ce titre.")   # évite les extraits de 30 s
    else:
        best = ok[0]
    return best["_url"]


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


def _download_url(url, query=None):
    folder = mkdtemp(prefix="music_")
    try:
        opts = ydl_base(folder)
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
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


def download_audio(query=None, expected=None, url=None):
    """Lien direct, sinon YouTube puis SoundCloud en secours."""
    if url:
        return _download_url(url, query)
    last = None
    for prefix in ("ytsearch5", "scsearch5"):
        try:
            return _download_url(pick_source(query, expected, prefix), query)
        except BotError as exc:
            last = exc
            log.warning("Source %s inutilisable : %s", prefix, exc)
    raise last or BotError("Aucune source disponible pour ce titre.")


def download_video(url):
    folder = mkdtemp(prefix="video_")
    try:
        opts = ydl_base(folder)
        opts["format"] = ("b[ext=mp4][height<=720]/bv*[ext=mp4][height<=720]+ba[ext=m4a]/"
                          "b[ext=mp4]/best[height<=720]/best")
        opts["merge_output_format"] = "mp4"
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
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
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise BotError("La vidéo dépasse la limite de %d Mo." % MAX_FILE_MB)
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

def fetch_cover(tr, folder):
    if not tr.get("art"):
        return None
    url = tr["art"].replace("100x100bb", "300x300bb")
    path = os.path.join(folder, "cover.jpg")
    try:
        with urlopen(Request(url, headers={"User-Agent": "MusicBotV3/3.0"}), timeout=8) as resp:
            blob = resp.read(250000)
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
    data = download_audio(query="%s %s" % (tr["artist"], tr["title"]), expected=tr.get("dur"))
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


def push_audio(chat_id, tr, data, caption, kb=None):
    """Envoie l'audio puis supprime toujours le dossier temporaire."""
    common = {
        "caption": caption[:1000], "parse_mode": "HTML", "reply_markup": kb,
        "title": tr["title"][:200], "performer": tr["artist"][:100], "timeout": 180,
    }
    try:
        if data.get("file_id"):
            try:
                bot.send_audio(chat_id, data["file_id"], **common)
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
                bot.send_document(chat_id, doc, caption=caption[:1000], parse_mode="HTML", timeout=180)
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
    if not tr.get("album_id"):
        return None
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💿 Télécharger l'album", callback_data="ab:%s" % tr["album_id"]))
    return kb


# ============================================================
# TÂCHES
# ============================================================

def job_track(chat_id, track_id):
    status = None
    try:
        status = send(chat_id, "⏳ <i>Préparation…</i>")
        tr = lookup_track(track_id)
        if not tr:
            raise BotError("Ce titre n'est plus disponible.")
        edit(chat_id, status.message_id, "⏳ <i>Je prépare <b>%s</b> — %s…</i>" % (esc(tr["title"]), esc(tr["artist"])))
        data = fetch_audio(tr)
        push_audio(chat_id, tr, data, track_caption(tr), album_kb(tr))
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
        data = download_audio(url=url)
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
        status = send(chat_id, "🎬 <i>Je traite ton lien…</i>")
        data = download_video(url)
        with open(data["path"], "rb") as video:
            bot.send_video(
                chat_id, video, caption=("🎬 <b>%s</b>" % esc(data["title"]))[:1000],
                duration=data.get("duration"), supports_streaming=True,
                parse_mode="HTML", timeout=180,
            )
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


def job_album(chat_id, album_id):
    status = None
    futures = {}
    try:
        status = send(chat_id, "💿 <i>Je récupère la liste des pistes…</i>")
        try:
            album, tracks = album_tracks(album_id)
        except Exception:
            raise BotError("Impossible de récupérer cet album pour le moment.")
        if not tracks:
            raise BotError("Cet album ne contient aucune piste exploitable.")

        total_all = len(tracks)
        tracks = tracks[:MAX_ALBUM_TRACKS]
        total = len(tracks)
        name = album.get("collectionName") or "Album"
        ahead = 3

        def prefetch(i):
            if i < total and i not in futures:
                futures[i] = album_pool.submit(safe_fetch, tracks[i])

        for i in range(min(ahead, total)):
            prefetch(i)

        ok, failed = 0, []
        for i, tr in enumerate(tracks):
            prefetch(i + ahead)
            edit(chat_id, status.message_id,
                 "💿 <b>%s</b>\n⏬ Piste <b>%d/%d</b> : %s" % (esc(name), i + 1, total, esc(tr["title"])))
            data, err = futures.pop(i).result()
            if err:
                failed.append(tr["title"])
                continue
            try:
                caption = "💿 <b>%d/%d</b> · %s\n👤 %s" % (i + 1, total, esc(tr["title"]), esc(tr["artist"]))
                push_audio(chat_id, tr, data, caption)
                ok += 1
            except Exception:
                log.exception("Envoi piste échoué : %s", tr["title"])
                failed.append(tr["title"])

        text = "✅ <b>%s</b> : %d/%d piste(s) envoyée(s)." % (esc(name), ok, total)
        if total_all > total:
            text += "\n<i>Limité à %d pistes sur %d.</i>" % (total, total_all)
        if failed:
            text += "\n⚠️ Échec : " + esc(", ".join(failed[:5]))
        edit(chat_id, status.message_id, text)
    except Exception as exc:
        report(chat_id, status, exc)
    finally:
        for fut in futures.values():
            try:
                fut.cancel()
                if fut.done() and not fut.cancelled():
                    cleanup((fut.result() or (None, None))[0])
            except Exception:
                pass


# ============================================================
# RECHERCHE
# ============================================================

def show_results(chat_id, query):
    status = send(chat_id, "🔎 <i>Je cherche <b>%s</b>…</i>" % esc(query))
    set_state(chat_id, query=query)
    kb = types.InlineKeyboardMarkup(row_width=1)
    try:
        items = search_tracks(query)
    except Exception:
        log.exception("Recherche iTunes échouée")
        items = None

    if items:
        set_state(chat_id, artist=items[0]["artist"])
        for tr in items:
            label = ("%s — %s" % (tr["title"], tr["artist"]))[:60]
            kb.add(types.InlineKeyboardButton(label, callback_data="tr:%s" % tr["id"]))
        text = "🔎 Résultats pour <b>%s</b>\n<i>Touche un titre pour le télécharger.</i>" % esc(query)
    else:
        text = "⚠️ Aucun résultat dans le catalogue pour <b>%s</b>." % esc(query)
        if items is None:
            text = "⚠️ Catalogue momentanément indisponible. Tu peux tenter la recherche directe."
    kb.row(
        types.InlineKeyboardButton("💿 Albums", callback_data="sa"),
        types.InlineKeyboardButton("▶️ Recherche directe", callback_data="yt"),
    )
    edit(chat_id, status.message_id, text, reply_markup=kb)


# ============================================================
# HANDLERS
# ============================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    clear_state(message.chat.id)
    name = message.from_user.first_name if message.from_user else "ami"
    send(message.chat.id,
         "👋 <b>Bonjour %s !</b>\n\n"
         "🎵 Écris un <b>titre</b> ou <b>artiste titre</b> : je te propose les résultats.\n"
         "🔗 Envoie un <b>lien</b> : je te donne l'audio ou la vidéo.\n"
         "💿 Après un titre, touche « Télécharger l'album »." % esc(name))


@bot.message_handler(commands=["aide", "help"])
def cmd_help(message):
    send(message.chat.id,
         "🎧 <b>Aide</b>\n\n"
         "• Texte → résultats cliquables\n"
         "• 💿 Albums → liste des albums\n"
         "• Lien → audio 🎵 ou vidéo 🎬\n"
         "• Tu peux enchaîner plusieurs demandes, elles passent en file.")


@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    try:
        chat_id = call.message.chat.id
    except Exception:
        return
    data = call.data or ""

    def queue(fn, *args, msg="Préparation…"):
        if submit(chat_id, fn, *args):
            answer(call, msg)
        else:
            answer(call, "Déjà %d tâches en cours, patiente un instant." % MAX_PENDING, alert=True)

    if data.startswith("tr:"):
        tid = data[3:]
        if not tid.isdigit():
            return answer(call, "Titre invalide.", alert=True)
        return queue(job_track, chat_id, tid)

    if data.startswith("ab:"):
        aid = data[3:]
        if not aid.isdigit():
            return answer(call, "Album invalide.", alert=True)
        return queue(job_album, chat_id, aid, msg="Album lancé…")

    if data == "sa":
        st = get_state(chat_id)
        term = st.get("artist") or st.get("query")
        if not term:
            return answer(call, "La recherche a expiré, renvoie ton titre.", alert=True)
        answer(call, "Recherche des albums…")
        try:
            albums = search_albums(term)
        except Exception:
            return answer(call, "Catalogue indisponible.", alert=True)
        if not albums:
            return answer(call, "Aucun album trouvé.", alert=True)
        tags = {0: "💿", 1: "📀 EP", 2: "🎵 Single"}
        kb = types.InlineKeyboardMarkup(row_width=1)
        for al in albums:
            label = "%s %s · %s" % (tags[al["kind"]], al["name"][:26], al["year"])
            kb.add(types.InlineKeyboardButton(label.strip(" ·"), callback_data="ab:%s" % al["id"]))
        send(chat_id,
             "💿 Albums de <b>%s</b>\n<i>Touche-en un pour le recevoir.</i>" % esc(term), reply_markup=kb)
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

    if URL_RE.match(text):
        set_state(chat_id, url=text[:2000])
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🎵 Audio", callback_data="da"),
            types.InlineKeyboardButton("🎬 Vidéo", callback_data="dv"),
        )
        send(chat_id, "🔗 Que veux-tu récupérer ?", reply_markup=kb)
        return

    show_results(chat_id, text[:200])


# ============================================================
# MAIN
# ============================================================

def main():
    setup_cookies()
    cache_load()
    threading.Thread(target=start_web, daemon=True, name="web").start()
    threading.Thread(target=keep_alive, daemon=True, name="keepalive").start()

    try:
        bot.remove_webhook()
    except Exception:
        log.exception("remove_webhook a échoué")
    try:
        bot.set_my_commands([
            types.BotCommand("start", "Ouvrir le menu"),
            types.BotCommand("aide", "Afficher l'aide"),
        ])
    except Exception:
        log.exception("set_my_commands a échoué")

    log.info("MusicBot V3 démarré (ffmpeg=%s).", "oui" if FFMPEG else "non")

    while True:
        try:
            bot.infinity_polling(
                timeout=20, long_polling_timeout=20, skip_pending=True,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as exc:
            log.warning("Coupure détectée (%s), reprise dans 5 s…", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
