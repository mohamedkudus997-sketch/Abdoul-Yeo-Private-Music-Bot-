"""Recherche de titres « façon YouTube Music » : tolérante aux fautes, artistes peu connus compris.

- YouTube Music (via ytmusicapi, sans compte) corrige déjà les fautes de frappe côté Google.
- Chaque résultat est ensuite noté par nos soins (mots de la demande retrouvés dans artiste + titre,
  avec tolérance orthographique) pour décider : télécharger tout de suite, proposer un choix, ou abandonner.
Aucun import obligatoire : si ytmusicapi est absent ou injoignable, le bot retombe sur ses autres sources.
"""
import difflib
import logging
import re
import threading
import unicodedata

log = logging.getLogger("musicbot")

try:
    from ytmusicapi import YTMusic
except Exception:           # paquet non installé : fonction désactivée, jamais bloquant
    YTMusic = None

AUTO_MIN = 0.80             # score à partir duquel on télécharge sans demander
CHOICE_MIN = 0.50           # en dessous, le résultat n'est même pas proposé
BAD_WORDS = ("remix", "live", "cover", "sped up", "speed up", "slowed", "reverb", "nightcore",
             "karaoke", "instrumental", "8d", "bass boosted", "mashup", "type beat")

_client = None
_lock = threading.Lock()
_store = {}                 # "ym:<videoId>" -> fiche titre (les boutons Telegram sont limités à 64 octets)
_store_lock = threading.Lock()


def available():
    return YTMusic is not None


def _yt():
    global _client
    if YTMusic is None:
        return None
    with _lock:
        if _client is None:
            try:
                _client = YTMusic()
            except Exception:
                log.warning("YouTube Music indisponible", exc_info=True)
                return None
        return _client


# ---------- comparaison de textes ----------

def plain(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _sim(a, b):
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def score(query, artist, title):
    """0..1 : à quel point (artiste, titre) correspond à la demande, fautes de frappe tolérées."""
    q_str = plain(query)
    q = q_str.split()
    t_clean = re.sub(r"[\(\[].*?[\)\]]", "", title or "")
    a_tok, t_tok = plain(artist).split(), plain(t_clean).split()
    cand = a_tok + t_tok
    if not q or not cand:
        return 0.0
    cov_q = sum(max(_sim(x, y) for y in cand) for x in q) / len(q)      # ma demande est-elle couverte ?
    cov_c = sum(max(_sim(y, x) for x in q) for y in cand) / len(cand)   # pas trop de mots en plus ?
    a_s, t_s = " ".join(a_tok), " ".join(t_tok)
    whole = max(_sim(q_str, t_s), _sim(q_str, a_s + " " + t_s), _sim(q_str, t_s + " " + a_s))
    value = 0.6 * cov_q + 0.2 * cov_c + 0.2 * whole
    low = plain(title)
    for word in BAD_WORDS:
        if re.search(r"\b%s\b" % re.escape(word), low) and word not in q_str:
            value -= 0.25
    return max(0.0, min(1.0, value))


# ---------- YouTube Music ----------

def _parse_song(r):
    vid = r.get("videoId")
    if r.get("resultType") not in ("song", "video") or not vid:
        return None
    artists = [a.get("name") for a in (r.get("artists") or []) if a and a.get("name")]
    thumbs = r.get("thumbnails") or []
    dur = r.get("duration_seconds")
    if not dur and r.get("duration"):
        try:
            parts = [int(x) for x in str(r["duration"]).split(":")]
            dur = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
        except Exception:
            dur = None
    tr = {
        "id": "ym:%s" % vid, "vid": vid, "title": (r.get("title") or "").strip(),
        "artist": ", ".join(artists) or "Artiste inconnu",
        "album": ((r.get("album") or {}).get("name") or ""), "year": r.get("year") or "",
        "dur": dur, "art": thumbs[-1]["url"] if thumbs else None,
        "album_id": ("yb:%s" % r["album"]["id"]) if (r.get("album") or {}).get("id") else None, "artist_id": None,
        "src": "ytm", "kind": r.get("resultType"),
    }
    return tr if tr["title"] else None


def _search_songs(query, ignore_spelling=False, limit=10):
    yt = _yt()
    if yt is None:
        return []
    try:
        rows = yt.search(query, filter="songs", limit=limit, ignore_spelling=ignore_spelling)
    except Exception:
        log.warning("Recherche YouTube Music échouée pour %r", query, exc_info=True)
        return []
    return [t for t in (_parse_song(r) for r in rows or []) if t]


def remember(tr):
    with _store_lock:
        _store[tr["id"]] = tr
        if len(_store) > 4000:
            for key in list(_store)[:1000]:
                _store.pop(key, None)


def get(track_id):
    with _store_lock:
        return _store.get(track_id)


def _dedupe(scored):
    seen, out = set(), []
    for value, tr in scored:
        key = (plain(tr["title"]), plain(tr["artist"]))
        if key in seen:
            continue
        seen.add(key)
        out.append((value, tr))
    return out


def find(query, max_dur=None):
    """Retourne [(score, fiche)] triés, meilleurs d'abord (liste vide si rien de crédible)."""
    if not available():
        return []
    cleaned = " ".join(str(query or "").split())
    variants = [cleaned]
    if " - " in cleaned:
        variants.append(cleaned.replace(" - ", " "))
    pool, best = {}, 0.0

    def run(q, ignore):
        nonlocal best
        for tr in _search_songs(q, ignore):
            if max_dur and tr["dur"] and tr["dur"] > max_dur:
                continue
            s = score(cleaned, tr["artist"], tr["title"])
            if tr["id"] not in pool or s > pool[tr["id"]][0]:
                pool[tr["id"]] = (s, tr)
            best = max(best, s)

    run(variants[0], False)                       # correction orthographique de Google
    if best < AUTO_MIN:
        for v in variants:                        # puis mot à mot, sans correction (artistes rares)
            run(v, True)
    ranked = _dedupe(sorted(pool.values(), key=lambda x: -x[0]))
    ranked = [(s, t) for s, t in ranked if s >= CHOICE_MIN]
    for _, tr in ranked:
        remember(tr)
    return ranked


def decide(ranked):
    """'auto' (télécharger), 'choose' (proposer 3-5 titres) ou 'none'."""
    if not ranked:
        return "none"
    top = ranked[0][0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    if top >= AUTO_MIN and (top - second) >= 0.04:
        return "auto"
    if top >= 0.9 and (top - second) >= 0.0 and second < 0.9:
        return "auto"
    return "choose"


def artist_page(query):
    """Si la demande EST un nom d'artiste (même peu connu) : (nom, photo, [titres]). Sinon None."""
    yt = _yt()
    if yt is None:
        return None
    try:
        rows = yt.search(query, filter="artists", limit=3)
    except Exception:
        log.warning("Recherche d'artiste échouée pour %r", query, exc_info=True)
        return None
    q = plain(query)
    for r in rows or []:
        name = r.get("artist") or r.get("title") or ""
        if name and _sim(plain(name), q) >= 0.92:
            songs = []
            for tr in _search_songs(name, False, 20):
                if any(_sim(plain(a), plain(name)) >= 0.85 for a in tr["artist"].split(", ")):
                    songs.append(tr)
            if not songs:
                return None
            seen, uniq = set(), []
            for tr in songs:
                key = plain(tr["title"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(tr)
            for tr in uniq:
                remember(tr)
            thumbs = r.get("thumbnails") or []
            return name, (thumbs[-1]["url"] if thumbs else None), uniq[:6]
    return None


# ---------- albums (YouTube Music) ----------

def search_albums(query, limit=8):
    """Albums/EP/singles trouvés pour la demande, fautes tolérées. [{id,name,artist,year,type,art}]"""
    yt = _yt()
    if yt is None:
        return []
    try:
        rows = yt.search(query, filter="albums", limit=limit)
    except Exception:
        log.warning("Recherche d'albums échouée pour %r", query, exc_info=True)
        return []
    out, seen = [], set()
    for r in rows or []:
        bid = r.get("browseId")
        name = (r.get("title") or "").strip()
        if not bid or not name:
            continue
        artist = ", ".join(a.get("name") for a in (r.get("artists") or []) if a and a.get("name"))
        key = (plain(name), plain(artist))
        if key in seen:
            continue
        seen.add(key)
        thumbs = r.get("thumbnails") or []
        kind = r.get("type") or "Album"
        value = score(query, artist, name) + (0.05 if kind == "Album" else 0.0)
        out.append((value, {"id": bid, "name": name, "artist": artist, "year": r.get("year") or "",
                            "type": kind, "art": thumbs[-1]["url"] if thumbs else None}))
    out.sort(key=lambda x: -x[0])
    return [a for _, a in out]


def album_tracks(browse_id):
    """(infos album, [fiches titres]) : pistes officielles de l'album avec videoId et durée exactes."""
    yt = _yt()
    if yt is None:
        raise RuntimeError("YouTube Music indisponible")
    data = yt.get_album(browse_id)
    thumbs = data.get("thumbnails") or []
    art = thumbs[-1]["url"] if thumbs else None
    album_artists = ", ".join(a.get("name") for a in (data.get("artists") or []) if a and a.get("name"))
    tracks = []
    for t in data.get("tracks") or []:
        vid = t.get("videoId")
        if not vid or t.get("isAvailable") is False:
            continue
        artists = ", ".join(a.get("name") for a in (t.get("artists") or []) if a and a.get("name")) or album_artists
        tr = {"id": "ym:%s" % vid, "vid": vid, "title": (t.get("title") or "").strip(), "artist": artists or "Artiste inconnu",
              "album": data.get("title") or "", "year": data.get("year") or "", "dur": t.get("duration_seconds"),
              "art": art, "album_id": "yb:%s" % browse_id, "artist_id": None, "src": "ytm"}
        if tr["title"]:
            tracks.append(tr)
            remember(tr)
    return {"collectionName": data.get("title") or "Album", "artist": album_artists, "year": data.get("year")}, tracks
