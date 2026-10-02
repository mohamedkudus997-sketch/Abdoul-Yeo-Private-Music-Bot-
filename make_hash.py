# -*- coding: utf-8 -*-
"""
Génère les empreintes à mettre dans les variables d'environnement Render.
Utilisation (en local, jamais dans un chat) :  python make_hash.py
Le code est normalisé : seuls les lettres/chiffres comptent (espaces et tirets ignorés).
"""
import getpass
import extras  # noqa: E402  (utilise les mêmes règles de normalisation que le bot)

print("1) Code d'accès (celui demandé à l'accueil)")
code = getpass.getpass("   Nouveau code : ")
print("   ACCESS_CODE_HASH=" + extras.hash_secret(extras.norm_code(code)))
print()
print("2) Phrase secrète du propriétaire")
phrase = getpass.getpass("   Nouvelle phrase : ")
print("   OWNER_PHRASE_HASH=" + extras.hash_secret(extras.norm_phrase(phrase)))
