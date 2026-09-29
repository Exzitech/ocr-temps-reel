"""Test OCR de bout en bout avec le vrai Tesseract.

On fabrique une image ressemblant au fil de commandes du fournisseur (texte clair sur fond sombre),
on la passe dans le meme pretraitement et le meme appel pytesseract que le
pipeline, puis dans le parsing. Valide la whitelist, le --psm et les regex
contre une vraie sortie OCR.

Ignore automatiquement si Tesseract ou la langue ne sont pas disponibles.

Lancement (depuis tools/cout-plats) : python -m unittest tests.test_ocr_reel -v
"""
from __future__ import annotations

import unittest

import config as module_config
from ocr import ErreurOCR, _pretraiter, construire_config_tesseract, verifier_tesseract
from parsing import decouper_messages, parser_message

LIGNES = [
    "[14:30] 12 x [Gelee Myrtille] (2400 euros)",
    "[14:31] 3 x [Épices de Cayenne] (18000 euros)",
    "[14:31] Banque : + 15000 Euros (vente de Salade de fruits)",
    "[14:32] 1 x [Fleur de courgette] (750 euros)",
    "[14:32] 1 x [Fleur de courgette] (750 euros)",
]


def _image_chat(lignes, largeur=760, hauteur_ligne=26):
    """Reproduit l'allure du chat : texte clair sur fond sombre."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (largeur, hauteur_ligne * len(lignes) + 12), (24, 24, 28))
    dessin = ImageDraw.Draw(image)
    try:
        police = ImageFont.truetype("arial.ttf", 16)
    except OSError:  # pragma: no cover
        police = ImageFont.load_default()
    for indice, ligne in enumerate(lignes):
        dessin.text((8, 6 + indice * hauteur_ligne), ligne,
                    font=police, fill=(220, 220, 210))
    return image


class TestOCRReel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = module_config.charger()
        reglages = cls.config["ocr"]
        try:
            verifier_tesseract(
                cls.config.get("tesseract_cmd", ""),
                lang=reglages.get("lang", ""),
                tessdata_dir=reglages.get("tessdata_dir", ""),
            )
        except ErreurOCR as exc:
            raise unittest.SkipTest("Tesseract indisponible : %s" % exc)

    def _ocr(self, image) -> str:
        import pytesseract

        chemin = self.config.get("tesseract_cmd")
        if chemin:
            pytesseract.pytesseract.tesseract_cmd = chemin
        reglages = self.config["ocr"]
        options = construire_config_tesseract(reglages)
        traitee = _pretraiter(image, int(reglages.get("upscale", 2)))
        return pytesseract.image_to_string(
            traitee, lang=reglages.get("lang", "fra"), config=options)

    def test_lecture_complete_du_chat(self):
        texte = self._ocr(_image_chat(LIGNES))
        messages = decouper_messages(texte)
        self.assertEqual(len(messages), len(LIGNES),
                         "messages lus : %r" % (messages,))

        achats = [parser_message(m) for m in messages]
        # La ligne banque est la seule a ne pas etre un achat.
        self.assertEqual([a is not None for a in achats],
                         [True, True, False, True, True],
                         "achats : %r" % (messages,))

        self.assertEqual(achats[0].quantite, 12)
        self.assertEqual(achats[0].prix_total, 2400)
        self.assertEqual(achats[1].quantite, 3)
        self.assertEqual(achats[1].prix_total, 18000)
        # Les deux dernieres lignes sont identiques : deux transactions.
        self.assertEqual(achats[3].nom_item, achats[4].nom_item)
        self.assertEqual(achats[3].prix_total, 750)

    def test_nom_accentue_matche_la_recette(self):
        """Le nom OCR doit matcher le nom catalogue de recettes malgre les accents."""
        from catalogue import Ingredient, Recette
        from tracker import STATUT_COMPTE, Session

        texte = self._ocr(_image_chat([LIGNES[1]]))
        achat = parser_message(decouper_messages(texte)[0])
        self.assertIsNotNone(achat, "OCR : %r" % texte)

        session = Session(
            [Recette(1, "Test", [Ingredient(1, "Épices de Cayenne", 3)])],
            seuil_auto=90, seuil_confirmation=70)
        resultat = session.traiter_achat(achat)
        self.assertEqual(resultat.statut, STATUT_COMPTE,
                         "lu %r -> %r (%.0f)" % (achat.nom_item, resultat.ingredient,
                                                 resultat.score))
        self.assertEqual(session.quantite_cumulee["Épices de Cayenne"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
