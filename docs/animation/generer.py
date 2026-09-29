"""Genere docs/demo.gif : le vrai DedupeurDeFlux face a un fil de commandes qui defile.

Le fil d'achats fictif d'un restaurant chez son grossiste defile ; a chaque "capture", on lit les lignes visibles
en y injectant du bruit de lecture (accents perdus, 0 lu O...), comme l'OCR, et
on les donne a moteur/parsing.py:DedupeurDeFlux. Les compteurs du GIF sont ceux
du moteur, compares au nombre reel d'achats. Aucune capture d'ecran ni Tesseract :
seule la lecture OCR est simulee, la deduplication est le code livre.

    python docs/animation/generer.py        (depuis la racine du depot)

Polices : Consolas et Segoe UI (Windows) ; ailleurs, police par defaut de Pillow.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

RACINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RACINE / "moteur"))
from parsing import DedupeurDeFlux  # noqa: E402

SORTIE = RACINE / "docs" / "demo.gif"
LARGEUR, HAUTEUR = 900, 450
LIGNES_VISIBLES = 9

FOND, ENCRE, DOUX, FILET = "#f6f1e9", "#1b1a17", "#5f584e", "#d6cbb9"
ACCENT, VERT, ECRAN, TEXTE_ECRAN = "#b04a1f", "#3c8a55", "#1b1a17", "#e9e2d6"

ARTICLES = [
    ("Farine T55", 10, "184,00"),
    ("Beurre doux", 1, "7,90"),
    ("Crème fraîche", 10, "32,50"),
    ("Œufs frais", 100, "28,00"),
]
# Achats par capture ; 3 = rafale de trois achats IDENTIQUES dans la meme minute.
PLAN = [0, 1, 0, 2, 1, 0, 3, 0, 1, 2, 0, 1, 3, 1, 0, 2, 1, 0, 1, 3, 0, 2, 1, 0, 0]


def police(nom: str, taille: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(nom, taille)
    except OSError:
        return ImageFont.load_default()


MONO = police("consola.ttf", 15)
TITRE = police("seguisb.ttf", 15)
PETIT = police("segoeui.ttf", 13)
CHIFFRE = police("segoeuib.ttf", 40)


def bruit(ligne: str, hasard: random.Random) -> str:
    """Une erreur de lecture typique de l'OCR sur une ligne."""
    remplacements = [("é", "e"), ("0", "O"), ("l", "1"), ("è", "e"), (" x ", " × ")]
    possibles = [(a, b) for a, b in remplacements if a in ligne]
    if not possibles:
        return ligne
    a, b = hasard.choice(possibles)
    return ligne.replace(a, b, 1)


def main() -> None:
    hasard = random.Random(7)
    minute = 14 * 60 + 2
    chat: list[str] = []

    def achat(article: tuple[str, int, str]) -> str:
        nom, quantite, prix = article
        return "[%02d:%02d] %d x %s (%s €)" % (minute // 60, minute % 60, quantite, nom, prix)

    # Achats deja affiches avant le lancement : jamais comptes (amorcage).
    for _ in range(6):
        chat.append(achat(hasard.choice(ARTICLES)))
    minute += 1

    dedupeur = DedupeurDeFlux()
    dedupeur.amorcer()
    reels = comptes = lignes_bruitees = 0
    images: list[Image.Image] = []

    for etape, nombre in enumerate(PLAN):
        if etape and etape % 3 == 0:
            minute += 1
        legende = ""
        if nombre == 3:
            chat += [achat(ARTICLES[0])] * 3
            legende = "Rafale : 3 achats identiques, comptés 3 fois"
        else:
            chat += [achat(hasard.choice(ARTICLES)) for _ in range(nombre)]
        reels += nombre

        visibles = chat[-LIGNES_VISIBLES:]
        lues = list(visibles)
        if hasard.random() < 0.6:
            i = hasard.randrange(len(lues))
            lues[i] = bruit(lues[i], hasard)
            lignes_bruitees += 1
        nouveaux = dedupeur.nouveaux(lues)
        comptes += len(nouveaux)
        images.append(dessiner(lues, len(nouveaux), reels, comptes, lignes_bruitees, legende))

    if comptes != reels:
        raise SystemExit(f"Ecart : {reels} achats reels, {comptes} comptes")
    # Derniere image affichee plus longtemps.
    durees = [900] * (len(images) - 1) + [3000]
    images[0].save(SORTIE, save_all=True, append_images=images[1:], duration=durees,
                   loop=0, optimize=True)
    print(f"{SORTIE} : {len(images)} images, {reels} achats reels, {comptes} comptes")


def dessiner(lues: list[str], nb_nouveaux: int, reels: int, comptes: int,
             bruitees: int, legende: str) -> Image.Image:
    image = Image.new("RGB", (LARGEUR, HAUTEUR), FOND)
    d = ImageDraw.Draw(image)

    d.text((24, 18), "Logiciel du grossiste : zone lue par l'OCR", font=TITRE, fill=ENCRE)
    d.rounded_rectangle((24, 48, 560, 404), radius=10, fill=ECRAN)
    for x in range(20, 566, 12):  # cadre pointille de la zone de capture
        d.line((x, 42, x + 6, 42), fill=ACCENT, width=2)
        d.line((x, 410, x + 6, 410), fill=ACCENT, width=2)
    for y in range(42, 412, 12):
        d.line((18, y, 18, y + 6), fill=ACCENT, width=2)
        d.line((566, y, 566, y + 6), fill=ACCENT, width=2)

    debut_nouveaux = len(lues) - nb_nouveaux
    for i, ligne in enumerate(lues):
        y = 62 + i * 37
        if i >= debut_nouveaux:
            d.rounded_rectangle((34, y - 5, 550, y + 25), radius=6, fill="#23402d")
            d.text((516, y + 1), "+1", font=TITRE, fill="#7fd39a")
        d.text((44, y), ligne, font=MONO, fill=TEXTE_ECRAN)

    x = 600
    d.text((x, 18), "Moteur de déduplication", font=TITRE, fill=ENCRE)
    d.text((x, 56), "Achats réels", font=PETIT, fill=DOUX)
    d.text((x, 72), str(reels), font=CHIFFRE, fill=ENCRE)
    d.text((x + 150, 56), "Comptés", font=PETIT, fill=DOUX)
    d.text((x + 150, 72), str(comptes), font=CHIFFRE, fill=VERT if comptes == reels else ACCENT)
    d.line((x, 140, 876, 140), fill=FILET)
    d.text((x, 154), "Comptés deux fois : 0", font=PETIT, fill=ENCRE)
    d.text((x, 178), f"Lignes mal lues tolérées : {bruitees}", font=PETIT, fill=ENCRE)
    d.text((x, 202), "Achats déjà affichés au départ : ignorés", font=PETIT, fill=ENCRE)
    if legende:
        d.rounded_rectangle((x, 240, 876, 276), radius=18, fill=ACCENT)
        d.text((x + 14, 248), legende, font=PETIT, fill=FOND)
    d.text((x, 372), "Déduplication : moteur/parsing.py", font=PETIT, fill=DOUX)
    d.text((x, 390), "Lecture OCR simulée, avec erreurs", font=PETIT, fill=DOUX)
    return image


if __name__ == "__main__":
    main()
