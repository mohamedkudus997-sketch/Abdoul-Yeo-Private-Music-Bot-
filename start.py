"""Démarrage : met à jour yt-dlp (YouTube change souvent) puis lance le bot."""
import os
import subprocess
import sys

base = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-U", "--quiet"]
try:
    subprocess.run(base + ["yt-dlp[default]", "ytmusicapi"], timeout=150, check=False)
    # Version « nightly » de yt-dlp : correctifs YouTube plus rapides (YTDLP_NIGHTLY=0 sur Render pour la désactiver).
    # --no-deps : seule yt-dlp passe en pré-version, pas ses dépendances.
    if os.environ.get("YTDLP_NIGHTLY", "1") != "0":
        subprocess.run(base + ["--pre", "--no-deps", "yt-dlp"], timeout=120, check=False)
except Exception as exc:  # pas bloquant
    print("Mise à jour yt-dlp ignorée :", exc)

import main  # noqa: E402

main.main()
