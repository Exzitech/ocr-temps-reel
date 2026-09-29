"""Outil de reglage de la zone de capture.

Capture la zone definie dans config.json, enregistre l'image (brute et
pretraitee) et affiche ce que l'OCR y lit, decoupe en messages.

    python outil_zone.py                 -> utilise config.json
    python outil_zone.py 20 780 900 260  -> left top width height (test rapide)

Ajustez capture.zone dans config.json jusqu'a ce que les messages soient lus
correctement, puis relancez le backend depuis ExziHub.
"""
from __future__ import annotations

import sys

import config as module_config
from ocr import (_capteur_mss, _pretraiter, construire_config_tesseract,
                 verifier_tesseract, verifier_zone)
from parsing import decouper_messages, parser_message


def main() -> int:
    configuration = module_config.charger()

    if len(sys.argv) == 5:
        gauche, haut, largeur, hauteur = (int(v) for v in sys.argv[1:5])
        configuration["capture"]["zone"] = {
            "left": gauche, "top": haut, "width": largeur, "height": hauteur
        }

    print("Zone testee :", configuration["capture"]["zone"])
    boite = verifier_zone(configuration["capture"])
    reglages_ocr = configuration["ocr"]
    version = verifier_tesseract(
        configuration.get("tesseract_cmd", ""),
        lang=reglages_ocr.get("lang", ""),
        tessdata_dir=reglages_ocr.get("tessdata_dir", ""),
    )
    print("Tesseract", version, "- langue", reglages_ocr.get("lang"))

    import mss
    import pytesseract
    from PIL import Image

    with _capteur_mss(mss)() as capteur:
        brut = capteur.grab(boite)
    image = Image.frombytes("RGB", brut.size, brut.bgra, "raw", "BGRX")
    image.save("zone_brute.png")
    traitee = _pretraiter(image, int(configuration["ocr"].get("upscale", 2)))
    traitee.save("zone_traitee.png")
    print("Images ecrites : zone_brute.png, zone_traitee.png")

    ocr = configuration["ocr"]
    reglages = construire_config_tesseract(ocr)
    texte = pytesseract.image_to_string(traitee, lang=ocr.get("lang", "fra"), config=reglages)

    print("\n--- TEXTE OCR BRUT ---")
    print(texte)
    print("--- MESSAGES DECOUPES ---")
    for message in decouper_messages(texte):
        achat = parser_message(message)
        marque = "ACHAT" if achat else "  -  "
        print("%s %s" % (marque, message))
        if achat:
            print("        -> %d x %r = %d euros"
                  % (achat.quantite, achat.nom_item, achat.prix_total))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Echec :", exc)
        sys.exit(1)
