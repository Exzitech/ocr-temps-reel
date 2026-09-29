"""Journal du moteur Coût des plats : cout-plats.log, dans le dossier de donnees.

Utilise par serveur.py.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys

from donnees import chemin_donnees

FICHIER_DEFAUT = "cout-plats.log"


def chemin_journal(config: dict) -> str:
    return chemin_donnees((config.get("log") or {}).get("fichier") or FICHIER_DEFAUT)


def configurer_logs(config: dict) -> None:
    niveau = getattr(logging, str((config.get("log") or {}).get("niveau", "INFO")).upper(),
                     logging.INFO)
    chemin = chemin_journal(config)
    handlers = []
    # Sous pythonw (lancement par le hub, sans console), sys.stdout vaut None.
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    try:
        # Rotation : le journal en DEBUG depasse vite des dizaines de milliers
        # de lignes (texte OCR complet a chaque capture).
        handlers.append(logging.handlers.RotatingFileHandler(
            chemin, maxBytes=5_000_000, backupCount=2, encoding="utf-8"))
    except OSError as exc:
        if sys.stdout is not None:
            print("Log fichier indisponible (%s) : %s" % (chemin, exc))
    logging.basicConfig(
        level=niveau,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
