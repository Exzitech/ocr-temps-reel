"""Ou le moteur ecrit ce qui doit survivre a une mise a jour du plugin.

Le hub remplace entierement plugins/<id>/ a chaque nouvelle version : tout ce
qui y serait ecrit (reglages, pack de langue, journaux) disparaitrait. Il passe
donc au moteur, dans EXZIHUB_DONNEES, un dossier a lui, hors de plugins/.

Repli sur le dossier du code quand on lance le moteur a la main, sans le hub
(outil_zone.py, demo_chat.py, les tests).
"""
from __future__ import annotations

import os

DOSSIER_CODE = os.path.dirname(os.path.abspath(__file__))
DOSSIER_DONNEES = os.environ.get("EXZIHUB_DONNEES") or DOSSIER_CODE
os.makedirs(DOSSIER_DONNEES, exist_ok=True)


def chemin_donnees(nom: str) -> str:
    """Chemin absolu d'un fichier de donnees ; un chemin deja absolu est rendu tel quel."""
    return nom if os.path.isabs(nom) else os.path.join(DOSSIER_DONNEES, nom)
