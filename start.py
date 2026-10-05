"""Démarrage : met à jour yt-dlp (YouTube change souvent) puis lance le bot."""
import subprocess
import sys

try:
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-U", "--quiet", "yt-dlp[default]", "ytmusicapi"],
                   timeout=120, check=False)
except Exception as exc:  # pas bloquant
    print("Mise à jour yt-dlp ignorée :", exc)

import main  # noqa: E402

main.main()
