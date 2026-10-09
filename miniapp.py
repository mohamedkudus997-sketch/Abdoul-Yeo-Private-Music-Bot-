"""
miniapp.py — Mini App Telegram de Téo (liquide-glace) : bibliothèque, lecteur, panneau propriétaire.

Sécurité : chaque appel doit prouver qu'il vient de Telegram (initData signé avec le jeton du bot).
Rien n'est renvoyé sans accès valide ; le panneau utilisateurs est réservé au propriétaire ;
un titre ne peut être écouté que s'il est dans la bibliothèque (ou les favoris) de l'utilisateur.
"""
import hashlib
import hmac
import io
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from urllib.parse import parse_qsl

import requests

log = logging.getLogger("musicbot.miniapp")

HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp.html")
INIT_MAX_AGE = 24 * 3600
AUDIO_CACHE_MAX = 6                      # fichiers gardés en mémoire pour la lecture (RAM limitée)
TG_FILE_LIMIT = 20 * 1024 * 1024         # limite de getFile côté Telegram


def validate_init_data(init_data, token, max_age=INIT_MAX_AGE, now=None):
    """Retourne le dict utilisateur Telegram si la signature est valide, sinon None."""
    try:
        pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
        got = pairs.pop("hash", None)
        if not got:
            return None
        check = "\n".join("%s=%s" % (k, pairs[k]) for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if max_age and abs((now or time.time()) - int(pairs.get("auth_date", "0"))) > max_age:
            return None
        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None


def register(app, bot, token, extras, cache_get, fetch_tg_file=None):
    """Branche les routes sur l'application Flask existante."""
    from flask import Response, jsonify, request, send_file
    audio_cache = OrderedDict()
    audio_lock = threading.Lock()

    def _download(file_id):
        if fetch_tg_file:
            return fetch_tg_file(file_id)
        info = bot.get_file(file_id)
        if info.file_size and info.file_size > TG_FILE_LIMIT:
            raise ValueError("fichier trop lourd pour la lecture")
        resp = requests.get("https://api.telegram.org/file/bot%s/%s" % (token, info.file_path), timeout=60)
        resp.raise_for_status()
        return resp.content

    def _audio_bytes(file_id):
        with audio_lock:
            if file_id in audio_cache:
                audio_cache.move_to_end(file_id)
                return audio_cache[file_id]
        data = _download(file_id)
        with audio_lock:
            audio_cache[file_id] = data
            while len(audio_cache) > AUDIO_CACHE_MAX:
                audio_cache.popitem(last=False)
        return data

    def _auth(raw=None):
        init = raw if raw is not None else (request.headers.get("X-Init-Data") or request.args.get("d") or "")
        user = validate_init_data(init, token)
        if not user:
            return None, ("Accès refusé", 401)
        uid = user["id"]
        try:
            extras.touch(uid, user.get("first_name"))
        except Exception:
            pass
        if not extras.is_ok(uid):
            return None, ("Accès non autorisé", 403)
        return user, None

    @app.get("/app")
    def mini_app():
        try:
            with open(HTML_FILE, "r", encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            return "Mini App indisponible", 404
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/me")
    def api_me():
        user, err = _auth()
        if err:
            return err
        uid = user["id"]
        status = extras.status_of(uid)
        row = next((u for u in extras.users_overview() if u["uid"] == uid), None)
        return jsonify({
            "id": uid, "name": user.get("first_name") or "", "owner": status == "owner",
            "days_left": row["days_left"] if row else None,
            "role": "owner" if status == "owner" else (row["role"] if row else "member"),
        })

    @app.post("/api/library")
    def api_library():
        user, err = _auth()
        if err:
            return err
        items = extras.library(user["id"])
        tracks, albums = [], OrderedDict()
        for it in items:
            playable = bool(cache_get(it["tid"]))
            t = dict(it, playable=playable)
            tracks.append(t)
            key = (it["album"] or "").strip()
            if key:
                a = albums.setdefault(key, {"name": key, "artist": it["artist"].split(",")[0], "art": it["art"],
                                            "tracks": []})
                a["tracks"].append(t)
        return jsonify({"tracks": tracks, "albums": list(albums.values())})

    @app.post("/api/users")
    def api_users():
        user, err = _auth()
        if err:
            return err
        if extras.status_of(user["id"]) != "owner":
            return "Réservé au propriétaire", 403
        users = extras.users_overview()
        return jsonify({"count": len(users), "users": users})

    @app.get("/api/audio/<path:tid>")
    def api_audio(tid):
        user, err = _auth()
        if err:
            return err
        uid = user["id"]
        allowed = any(it["tid"] == tid for it in extras.library(uid)) or bool(extras.is_fav(uid, tid))
        if not allowed:
            return "Titre absent de ta bibliothèque", 403
        file_id = cache_get(tid)
        if not file_id:
            return "Fichier indisponible : retélécharge ce titre depuis le bot", 404
        try:
            data = _audio_bytes(file_id)
        except Exception as exc:
            log.warning("Lecture impossible (%s) : %s", tid, exc)
            return "Lecture impossible pour ce titre", 502
        mime = "audio/mpeg" if (data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3")) else "audio/mp4"
        return send_file(io.BytesIO(data), mimetype=mime, conditional=True, max_age=0)

    return app
