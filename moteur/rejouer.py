"""Rejeu hors ligne des textes OCR enregistres (capture.enregistrer_textes).

Active dans config.json : "capture": {"enregistrer_textes": "captures-ocr.jsonl"}
puis joue une session. Ensuite :

    python rejouer.py captures-ocr.jsonl

rejoue chaque texte dans le suivi de flux (DedupeurDeFlux) et affiche les
achats detectes, sans Tesseract ni le logiciel du fournisseur : on peut ainsi tester un changement
de parsing/deduplication sur une vraie session.
"""
from __future__ import annotations

import json
import sys

from parsing import DedupeurDeFlux, parser_capture, parser_message


def rejouer(chemin: str) -> int:
    dedupeur = DedupeurDeFlux()
    total = 0
    with open(chemin, encoding="utf-8") as fichier:
        for ligne in fichier:
            entree = json.loads(ligne)
            if entree.get("source") == "securite":
                continue  # la zone de securite ne touche pas au suivi positionnel
            for message in dedupeur.nouveaux(parser_capture(entree["texte"])):
                achat = parser_message(message)
                if achat:
                    total += 1
                    print("[%s] %d x %s : %d k" % (
                        achat.heure, achat.quantite, achat.nom_item, achat.prix_total))
    print("%d achat(s) detecte(s)." % total)
    return total


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    rejouer(sys.argv[1])
