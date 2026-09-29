# -*- coding: utf-8 -*-
"""
MusicBot V2 — Telegram + Render + PostgreSQL

Configuration par variables d'environnement :
BOT_TOKEN          obligatoire
DATABASE_URL       obligatoire
PORT               fourni par Render (10000 par défaut)
MAX_FILE_MB        49 par défaut
MAX_DURATION       900 secondes par défaut
MAX_ALBUM_TRACKS   10 par défaut
COOLDOWN_SECONDS   5 par défaut
WORKERS            3 par défaut
ITUNES_COUNTRY     FR par défaut

Le conteneur Render installe FFmpeg + Node.js afin que yt-dlp puisse
convertir l'audio en M4A et fusionner les formats vidéo si nécessaire.
"""

import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from tempfile import mkdtemp
import shutil
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg
from psycopg_pool import ConnectionPool
from flask import Flask
from PIL import Image
import telebot
from telebot import types
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN est manquant. Ajoute BOT_TOKEN dans Render.")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL est manquant. Connecte une base PostgreSQL Render "
        "et ajoute son Internal Database URL."
    )

PORT = int(os.environ.get("PORT", "10000"))
MAX_FILE_MB = max(1, int(os.environ.get("MAX_FILE_MB", "49")))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_DURATION = max(30, int(os.environ.get("MAX_DURATION", "900")))
MAX_ALBUM_TRACKS = max(1, int(os.environ.get("MAX_ALBUM_TRACKS", "10")))
COOLDOWN = max(0.0, float(os.environ.get("COOLDOWN_SECONDS", "5")))
WORKERS = max(1, int(os.environ.get("WORKERS", "3")))
ITUNES_COUNTRY = os.environ.get("ITUNES_COUNTRY", "FR").upper()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("musicbot")

app = Flask(__name__)

bot = telebot.TeleBot(
    TOKEN,
    threaded=True,
    num_threads=8,
)

executor = ThreadPoolExecutor(
    max_workers=WORKERS,
    thread_name_prefix="download",
)


# ============================================================
# ETAT EN MEMOIRE
# ============================================================

state_lock = threading.Lock()
states = {}

cooldown_lock = threading.Lock()
last_request = {}

busy_lock = threading.Lock()
busy_chats = set()


def set_state(chat_id, **values):
    with state_lock:
        current = states.get(chat_id, {}).copy()
        current.update(values)
        states[chat_id] = current


def get_state(chat_id):
    with state_lock:
        return states.get(chat_id, {}).copy()


def clear_state(chat_id):
    with state_lock:
        states.pop(chat_id, None)


def acquire_chat(chat_id):
    with busy_lock:
        if chat_id in busy_chats:
            return False
        busy_chats.add(chat_id)
        return True


def release_chat(chat_id):
    with busy_lock:
        busy_chats.discard(chat_id)


def cooldown_ok(chat_id):
    now = time.monotonic()
    with cooldown_lock:
        previous = last_request.get(chat_id, 0.0)
        elapsed = now - previous
        if elapsed < COOLDOWN:
            return False, max(1, int(COOLDOWN - elapsed) + 1)
        last_request[chat_id] = now
        return True, 0


# ============================================================
# POSTGRESQL
# ============================================================

pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=max(2, WORKERS + 2),
    timeout=10,
    open=False,
)


@contextmanager
def db():
    with pool.connection() as conn:
        yield conn


def init_db(retries=5):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            pool.open(waiting=True)

            with db() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        chat_id BIGINT PRIMARY KEY,
                        first_name TEXT,
                        username TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS searches (
                        id BIGSERIAL PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        query TEXT NOT NULL,
                        artist TEXT,
                        title TEXT,
                        album TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )

            log.info("PostgreSQL connectée et tables vérifiées.")
            return

        except Exception as exc:
            last_error = exc
            log.exception(
                "Connexion PostgreSQL échouée (tentative %s/%s).",
                attempt,
                retries,
            )
            time.sleep(min(5 * attempt, 20))

    raise RuntimeError(
        f"Impossible de se connecter à PostgreSQL après {retries} tentatives: {last_error}"
    )


def save_user(message):
    try:
        user = message.from_user
        with db() as conn:
            conn.execute(
                """
                INSERT INTO users(chat_id, first_name, username)
                VALUES (%s, %s, %s)
                ON CONFLICT(chat_id) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    username = EXCLUDED.username,
                    updated_at = NOW()
                """,
                (
                    message.chat.id,
                    user.first_name if user else None,
                    user.username if user else None,
                ),
            )
    except Exception:
        # Une panne DB ne doit pas faire tomber le bot.
        log.exception("Impossible d'enregistrer l'utilisateur.")


def save_search(chat_id, query, artist=None, title=None, album=None):
    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO searches(chat_id, query, artist, title, album)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (chat_id, query, artist, title, album),
            )
    except Exception:
        log.exception("Impossible d'enregistrer la recherche.")


# ============================================================
# FLASK / RENDER
# ============================================================

@app.get("/")
def home():
    return "MusicBot V2 is running", 200


@app.get("/health")
def health():
    return "ok", 200


def start_web():
    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
        use_reloader=False,
    )


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
        return bot.edit_message_text(
            text,
            chat_id,
            message_id,
            **kwargs,
        )
    except Exception:
        return None


def answer(call, text="", alert=False):
    try:
        bot.answer_callback_query(
            call.id,
            text,
            show_alert=alert,
        )
    except Exception:
        pass


def music_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(
            "🎵 MUSIC",
            callback_data="mode_music",
        ),
        types.InlineKeyboardButton(
            "🎬 VIDEO",
            callback_data="mode_video",
        ),
    )
    return kb


def cancel_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "❌ Annuler",
            callback_data="cancel",
        )
    )
    return kb


def result_keyboard(album_id=None):
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.add(
        types.InlineKeyboardButton(
            "⬇️ Télécharger",
            callback_data="download_track",
        )
    )

    if album_id:
        kb.add(
            types.InlineKeyboardButton(
                "💿 Télécharger l'album",
                callback_data=f"album:{album_id}",
            )
        )

    kb.add(
        types.InlineKeyboardButton(
            "🔎 Nouvelle recherche",
            callback_data="mode_music",
        )
    )

    return kb


# ============================================================
# ITUNES
# ============================================================

def itunes(endpoint, params):
    url = (
        "https://itunes.apple.com/"
        + endpoint
        + "?"
        + urlencode(params)
    )

    req = Request(
        url,
        headers={"User-Agent": "MusicBotV2/2.0"},
    )

    with urlopen(req, timeout=12) as response:
        import json
        return json.loads(
            response.read().decode("utf-8")
        )


def search_itunes(query, artist=None):
    term = f"{artist} {query}".strip() if artist else query

    data = itunes(
        "search",
        {
            "term": term,
            "media": "music",
            "entity": "song",
            "limit": 20,
            "country": ITUNES_COUNTRY,
        },
    )

    results = data.get("results") or []

    if artist:
        target = artist.lower()
        results.sort(
            key=lambda item: (
                0
                if target in (item.get("artistName") or "").lower()
                else 1
            )
        )

    return results


def album_tracks(album_id):
    data = itunes(
        "lookup",
        {
            "id": album_id,
            "entity": "song",
            "country": ITUNES_COUNTRY,
        },
    )

    results = data.get("results") or []

    album = next(
        (
            item
            for item in results
            if item.get("wrapperType") == "collection"
        ),
        {},
    )

    tracks = [
        item
        for item in results
        if item.get("wrapperType") == "track"
        and item.get("kind") == "song"
    ]

    tracks.sort(
        key=lambda item: (
            item.get("discNumber", 1),
            item.get("trackNumber", 0),
        )
    )

    return album, tracks


# ============================================================
# YT-DLP
# ============================================================

AUDIO_EXTS = {
    ".m4a",
    ".mp3",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
    ".wav",
}

VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
}


def ydl_audio_opts(folder):
    return {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_FILE_BYTES,
        "overwrites": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "5",
            }
        ],
    }


def ydl_video_opts(folder):
    return {
        "format": (
            "bv*[ext=mp4]+ba[ext=m4a]/"
            "b[ext=mp4]/"
            "best"
        ),
        "outtmpl": os.path.join(
            folder,
            "%(id)s.%(ext)s",
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_FILE_BYTES,
        "overwrites": False,
    }


def find_media(folder, allowed):
    candidates = []

    for filename in os.listdir(folder):
        path = os.path.join(folder, filename)

        if (
            os.path.isfile(path)
            and os.path.splitext(filename)[1].lower()
            in allowed
        ):
            candidates.append(path)

    if not candidates:
        return None

    return max(
        candidates,
        key=os.path.getsize,
    )


def inspect_duration(ydl_options, target):
    with YoutubeDL(ydl_options) as ydl:
        info = ydl.extract_info(
            target,
            download=False,
        )

    if not info:
        raise RuntimeError(
            "Aucun résultat exploitable n'a été trouvé."
        )

    entries = info.get("entries")
    if entries:
        info = next(
            (entry for entry in entries if entry),
            None,
        )

    if not info:
        raise RuntimeError(
            "Aucun résultat exploitable n'a été trouvé."
        )

    duration = info.get("duration")

    if duration and duration > MAX_DURATION:
        raise RuntimeError(
            f"Ce média dure {int(duration // 60)} min "
            f"{int(duration % 60):02d} s. "
            f"La limite est de {MAX_DURATION // 60} min."
        )

    return info


def download_audio(query):
    folder = mkdtemp(prefix="music_")
    target = f"ytsearch1:{query}"
    try:
        preview = inspect_duration(ydl_audio_opts(folder), target)
        with YoutubeDL(ydl_audio_opts(folder)) as ydl:
            info = ydl.extract_info(target, download=True)
        if info and info.get("entries"):
            info = next((entry for entry in info["entries"] if entry), preview)
        path = find_media(folder, AUDIO_EXTS)
        if not path:
            raise RuntimeError("Le fichier audio n'a pas été produit.")
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise RuntimeError(f"Le fichier dépasse la limite de {MAX_FILE_MB} Mo.")
        return {
            "path": path,
            "title": (info.get("title") or query)[:200],
            "artist": (info.get("artist") or info.get("creator") or info.get("uploader") or "Artiste inconnu")[:100],
            "album": info.get("album"),
            "duration": int(info["duration"]) if info.get("duration") else None,
            "size": size,
            "folder": folder,
        }
    except DownloadError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError("Le service vidéo n'a pas pu récupérer ce titre pour le moment.") from exc
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def download_video(url):
    folder = mkdtemp(prefix="video_")
    try:
        info = inspect_duration(ydl_video_opts(folder), url)
        with YoutubeDL(ydl_video_opts(folder)) as ydl:
            info = ydl.extract_info(url, download=True)
        path = find_media(folder, VIDEO_EXTS)
        if not path:
            raise RuntimeError("Aucun fichier vidéo exploitable n'a été trouvé.")
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise RuntimeError(f"La vidéo dépasse la limite de {MAX_FILE_MB} Mo.")
        return {
            "path": path,
            "title": (info.get("title") or "Vidéo")[:200],
            "duration": int(info["duration"]) if info.get("duration") else None,
            "size": size,
            "folder": folder,
        }
    except DownloadError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError("Je n'ai pas pu récupérer ce média. Vérifie le lien puis réessaie.") from exc
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


# ============================================================
# MUSIQUE
# ============================================================

def search_and_show(chat_id):
    st = get_state(chat_id)

    title = st.get("title", "").strip()
    artist = st.get("artist", "").strip()

    status = send(
        chat_id,
        "🔎 <i>Je recherche "
        f"<b>{esc(title)}</b>"
        f"{' de ' + esc(artist) if artist else ''}…</i>",
    )

    try:
        results = search_itunes(
            title,
            artist,
        )
    except Exception:
        log.exception("iTunes search failed")
        edit(
            chat_id,
            status.message_id,
            "⚠️ Le service de recherche est momentanément "
            "indisponible. Réessaie plus tard.",
        )
        return

    if not results:
        edit(
            chat_id,
            status.message_id,
            "⚠️ Aucun résultat trouvé. "
            "Essaie avec le titre et l'artiste.",
        )
        return

    best = results[0]
    album_id = best.get("collectionId")

    set_state(
        chat_id,
        mode="music",
        step="result",
        title=best.get("trackName") or title,
        artist=best.get("artistName") or artist,
        album=best.get("collectionName"),
        album_id=str(album_id)
        if album_id
        else None,
        release_date=(
            best.get("releaseDate") or ""
        )[:4],
    )

    save_search(
        chat_id,
        title,
        best.get("artistName"),
        best.get("trackName"),
        best.get("collectionName"),
    )

    year = (
        best.get("releaseDate") or ""
    )[:4]

    album = (
        best.get("collectionName")
        or "Album inconnu"
    )

    text = (
        f"🎵 <b>{esc(best.get('trackName') or title)}</b>\n"
        f"👤 <b>{esc(best.get('artistName') or artist or 'Artiste inconnu')}</b>\n"
        f"💿 <b>{esc(album)}</b>"
    )

    if year:
        text += f"\n📅 {year}"

    text += "\n\n🫴 Voilà ce que j'ai trouvé."

    edit(
        chat_id,
        status.message_id,
        text,
        reply_markup=result_keyboard(album_id),
    )


def ask_for_artist(chat_id, title):
    set_state(
        chat_id,
        mode="music",
        step="waiting_artist",
        title=title,
    )

    send(
        chat_id,
        f"🎵 <b>{esc(title)}</b>\n\n"
        "👤 Quel est le nom de l'artiste ?",
        reply_markup=cancel_keyboard(),
    )


def process_track(chat_id):
    data = None
    try:
        st = get_state(chat_id)
        title = st.get("title", "").strip()
        artist = st.get("artist", "").strip()
        if not title or not artist:
            send(chat_id, "⚠️ Les informations de cette recherche ne sont plus disponibles.")
            return

        status = send(chat_id, f"⏳ <i>Je prépare <b>{esc(title)}</b> de <b>{esc(artist)}</b>…</i>")
        data = download_audio(f"{artist} {title}".strip())

        caption = f"🎵 <b>{esc(title)}</b>\n👤 {esc(artist)}"
        if data.get("album"):
            caption += f"\n💿 {esc(data['album'])}"

        with open(data["path"], "rb") as audio:
            bot.send_audio(
                chat_id,
                audio,
                title=title[:200],
                performer=artist[:100],
                duration=data.get("duration"),
                caption=caption[:1000],
                parse_mode="HTML",
            )

        edit(chat_id, status.message_id, "✅ Voilà pour toi !")

    except Exception as exc:
        log.exception("Track failed")
        send(chat_id, "⚠️ Je n'ai pas pu récupérer ce titre pour le moment.\n<i>%s</i>" % esc(str(exc)))
    finally:
        if data:
            shutil.rmtree(data.get("folder", ""), ignore_errors=True)
        release_chat(chat_id)


def process_album(chat_id, album_id):
    try:
        status = send(chat_id, "💿 <i>Je récupère la liste des pistes…</i>")
        try:
            album, tracks = album_tracks(album_id)
        except Exception:
            log.exception("Album lookup failed")
            edit(chat_id, status.message_id, "⚠️ Impossible de récupérer cet album pour le moment.")
            return

        if not tracks:
            edit(chat_id, status.message_id, "⚠️ Cet album ne contient aucune piste exploitable.")
            return

        tracks = tracks[:MAX_ALBUM_TRACKS]
        total = len(tracks)
        ok = 0

        for index, track in enumerate(tracks, 1):
            data = None
            title = track.get("trackName") or f"Piste {index}"
            artist = track.get("artistName") or album.get("artistName") or "Artiste inconnu"
            edit(chat_id, status.message_id,
                 f"💿 <b>{esc(album.get('collectionName') or 'Album')}</b>\n"
                 f"⏬ Piste <b>{index}/{total}</b> : {esc(title)}")
            try:
                data = download_audio(f"{artist} {title}")
                caption = f"💿 <b>Piste #{index}</b> — {esc(title)}\n👤 {esc(artist)}"
                with open(data["path"], "rb") as audio:
                    bot.send_audio(chat_id, audio, title=title[:200], performer=artist[:100],
                                   duration=data.get("duration"), caption=caption[:1000], parse_mode="HTML")
                ok += 1
            except Exception:
                log.exception("Album track failed: %s", title)
            finally:
                if data:
                    shutil.rmtree(data.get("folder", ""), ignore_errors=True)

        edit(chat_id, status.message_id, f"✅ Album terminé : <b>{ok}/{total}</b> piste(s) envoyée(s).")
    finally:
        release_chat(chat_id)


# ============================================================
# VIDEO
# ============================================================

def process_video(chat_id, url):
    data = None
    try:
        status = send(chat_id, "🎬 <i>Je traite ton lien…</i>")
        data = download_video(url)
        with open(data["path"], "rb") as video:
            bot.send_video(
                chat_id,
                video,
                caption=f"🎬 <b>{esc(data['title'])}</b>"[:1000],
                duration=data.get("duration"),
                supports_streaming=True,
                parse_mode="HTML",
            )
        edit(chat_id, status.message_id, "✅ Voilà ta vidéo.")
    except Exception as exc:
        log.exception("Video failed")
        send(chat_id, "⚠️ Je n'ai pas pu traiter ce lien.\n<i>%s</i>" % esc(str(exc)))
    finally:
        if data:
            shutil.rmtree(data.get("folder", ""), ignore_errors=True)
        release_chat(chat_id)


# ============================================================
# COMMANDES
# ============================================================

@bot.message_handler(commands=["start"])
def start(message):
    save_user(message)
    clear_state(message.chat.id)

    name = (
        message.from_user.first_name
        if message.from_user
        else "ami"
    )

    send(
        message.chat.id,
        f"👋 <b>Bonjour {esc(name)} !</b>\n\n"
        "🎧 Bienvenue sur ton assistant multimédia.\n"
        "Choisis un mode :",
        reply_markup=music_keyboard(),
    )


@bot.message_handler(commands=["aide", "help"])
def help_cmd(message):
    send(
        message.chat.id,
        "🎧 <b>Aide</b>\n\n"
        "🎵 <b>Music</b> : recherche un titre et son artiste.\n"
        "🎬 <b>Video</b> : traite un lien compatible.\n\n"
        "Utilise /start pour revenir au menu.",
    )


# ============================================================
# CALLBACKS
# ============================================================

@bot.callback_query_handler(
    func=lambda c: c.data == "mode_music"
)
def cb_music(call):
    answer(call)

    set_state(
        call.message.chat.id,
        mode="music",
        step="waiting_title",
    )

    send(
        call.message.chat.id,
        "🎵 <b>Mode Music</b>\n\n"
        "Quel titre souhaites-tu rechercher ?",
        reply_markup=cancel_keyboard(),
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "mode_video"
)
def cb_video(call):
    answer(call)

    set_state(
        call.message.chat.id,
        mode="video",
        step="waiting_url",
    )

    send(
        call.message.chat.id,
        "🎬 <b>Mode Video</b>\n\n"
        "Envoie-moi le lien du média.",
        reply_markup=cancel_keyboard(),
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "cancel"
)
def cb_cancel(call):
    answer(call, "Annulé")
    clear_state(call.message.chat.id)
    send(
        call.message.chat.id,
        "D'accord 👍",
        reply_markup=music_keyboard(),
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "download_track"
)
def cb_download(call):
    chat_id = call.message.chat.id

    st = get_state(chat_id)

    if (
        not st.get("title")
        or not st.get("artist")
    ):
        answer(
            call,
            "La recherche a expiré.",
            alert=True,
        )
        return

    ok, wait = cooldown_ok(chat_id)

    if not ok:
        answer(
            call,
            f"Patiente {wait}s.",
            alert=True,
        )
        return

    if not acquire_chat(chat_id):
        answer(
            call,
            "Une tâche est déjà en cours.",
            alert=True,
        )
        return

    answer(call, "Préparation…")
    executor.submit(
        process_track,
        chat_id,
    )


@bot.callback_query_handler(
    func=lambda c: c.data.startswith("album:")
)
def cb_album(call):
    chat_id = call.message.chat.id
    album_id = call.data.split(":", 1)[1]

    if not album_id.isdigit():
        answer(
            call,
            "Album invalide.",
            alert=True,
        )
        return

    ok, wait = cooldown_ok(chat_id)

    if not ok:
        answer(
            call,
            f"Patiente {wait}s.",
            alert=True,
        )
        return

    if not acquire_chat(chat_id):
        answer(
            call,
            "Une tâche est déjà en cours.",
            alert=True,
        )
        return

    answer(call, "Album lancé…")

    executor.submit(
        process_album,
        chat_id,
        album_id,
    )


# ============================================================
# TEXTES
# ============================================================

@bot.message_handler(content_types=["text"])
def text_message(message):
    save_user(message)

    chat_id = message.chat.id
    text = (message.text or "").strip()

    if not text or text.startswith("/"):
        return

    st = get_state(chat_id)
    mode = st.get("mode")
    step = st.get("step")

    if (
        mode == "video"
        and step == "waiting_url"
    ):
        if not re.match(
            r"^https?://",
            text,
            re.I,
        ):
            send(
                chat_id,
                "⚠️ Envoie un lien commençant par "
                "http:// ou https://.",
            )
            return

        if not acquire_chat(chat_id):
            send(
                chat_id,
                "⏳ Une tâche est déjà en cours.",
            )
            return

        clear_state(chat_id)

        executor.submit(
            process_video,
            chat_id,
            text[:2000],
        )
        return

    if (
        mode == "music"
        and step == "waiting_artist"
    ):
        artist = text[:100]

        set_state(
            chat_id,
            artist=artist,
            step="searching",
        )

        send(
            chat_id,
            f"👊 <b>{esc(st.get('title'))}</b> "
            f"de <b>{esc(artist)}</b>.\n"
            "⏳ Je lance la recherche…",
        )

        executor.submit(
            search_and_show,
            chat_id,
        )
        return

    if (
        mode == "music"
        and step == "waiting_title"
    ):
        ask_for_artist(
            chat_id,
            text[:200],
        )
        return

    # Hors conversation : texte = titre.
    ask_for_artist(
        chat_id,
        text[:200],
    )


# ============================================================
# MAIN
# ============================================================

def main():
    # Le serveur HTTP démarre immédiatement pour que Render
    # puisse détecter le port pendant que PostgreSQL est vérifiée.
    threading.Thread(
        target=start_web,
        daemon=True,
        name="render-web",
    ).start()

    init_db()

    try:
        bot.remove_webhook()
    except Exception:
        log.exception("remove_webhook a échoué")

    try:
        bot.set_my_commands(
            [
                types.BotCommand(
                    "start",
                    "Ouvrir le menu",
                ),
                types.BotCommand(
                    "aide",
                    "Afficher l'aide",
                ),
            ]
        )
    except Exception:
        log.exception(
            "Impossible de configurer les commandes Telegram."
        )

    log.info(
        "MusicBot V2 démarré | workers=%s | max_file=%sMB | max_duration=%ss",
        WORKERS,
        MAX_FILE_MB,
        MAX_DURATION,
    )

    try:
        bot.infinity_polling(
            timeout=30,
            long_polling_timeout=20,
            skip_pending=True,
            allowed_updates=[
                "message",
                "callback_query",
            ],
        )
    finally:
        executor.shutdown(
            wait=False,
            cancel_futures=True,
        )

        try:
            pool.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
