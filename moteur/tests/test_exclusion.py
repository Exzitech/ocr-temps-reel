"""Les fenetres de l'outil ne doivent JAMAIS etre relues par sa propre capture.

Bug reel : la console et les fenetres "toujours au-dessus" etaient posees sur
le fil de commandes. L'OCR relisait le journal "[22:36] 10 x [Filet de sanglier]
(3 993 euros)" comme un nouvel achat, ce qui produisait une nouvelle ligne de
journal, relue a son tour : un seul achat reel compte 9 fois, faux prix fige.

Lancement (depuis tools/cout-plats) : python -m unittest tests.test_exclusion -v
"""
from __future__ import annotations

import os
import time
import unittest

import config as module_config

LIGNE_PIEGE = "[12:34] 10 x [Piment cramoisi] (3 993 euros)"


def _affinite(fenetre) -> int:
    """Valeur de SetWindowDisplayAffinity appliquee a une fenetre Tk."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    fenetre.update_idletasks()
    racine = user32.GetAncestor(fenetre.winfo_id(), 2)
    valeur = wintypes.DWORD()
    user32.GetWindowDisplayAffinity(racine, ctypes.byref(valeur))
    return valeur.value


@unittest.skipUnless(os.name == "nt", "exclusion de capture propre a Windows")
class TestExclusionDeLaCapture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk

        try:
            cls.racine = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover
            raise unittest.SkipTest("Pas d'affichage : %s" % exc)
        cls.racine.withdraw()
        cls.config = module_config.charger()

    @classmethod
    def tearDownClass(cls):
        cls.racine.destroy()

    def _ocr_de(self, fenetre) -> str:
        import mss
        import pytesseract
        from PIL import Image

        from ocr import _capteur_mss, _pretraiter, construire_config_tesseract

        if self.config.get("tesseract_cmd"):
            pytesseract.pytesseract.tesseract_cmd = self.config["tesseract_cmd"]
        zone = {"left": fenetre.winfo_rootx(), "top": fenetre.winfo_rooty(),
                "width": fenetre.winfo_width(), "height": fenetre.winfo_height()}
        with _capteur_mss(mss)() as capteur:
            brut = capteur.grab(zone)
        image = Image.frombytes("RGB", brut.size, brut.bgra, "raw", "BGRX")
        return pytesseract.image_to_string(
            _pretraiter(image, 2), lang=self.config["ocr"]["lang"],
            config=construire_config_tesseract(self.config["ocr"]))

    def test_une_fenetre_masquee_n_est_plus_lue_comme_un_achat(self):
        import tkinter as tk

        from ocr import ErreurOCR, verifier_tesseract
        from ocr import exclure_de_la_capture
        from parsing import decouper_messages, parser_message

        try:
            verifier_tesseract(self.config.get("tesseract_cmd", ""),
                               self.config["ocr"]["lang"],
                               self.config["ocr"]["tessdata_dir"])
        except ErreurOCR as exc:
            self.skipTest("Tesseract indisponible : %s" % exc)

        piege = tk.Toplevel(self.racine)
        try:
            piege.geometry("720x110+500+300")
            piege.attributes("-topmost", True)
            tk.Label(piege, text=LIGNE_PIEGE, font=("Segoe UI", 16),
                     bg="white").pack(fill="both", expand=True)
            piege.update()
            time.sleep(0.4)
            piege.update()

            def achats_lus():
                texte = self._ocr_de(piege)
                return [a for a in map(parser_message, decouper_messages(texte)) if a]

            # Sans exclusion : l'outil se relit, la ligne devient un achat.
            avant = achats_lus()
            self.assertTrue(avant, "le piege devrait etre lisible sans exclusion")
            self.assertEqual(avant[0].prix_total, 3993)

            self.assertTrue(exclure_de_la_capture(piege.winfo_id()))
            piege.update()
            time.sleep(0.6)
            piege.update()

            # Avec exclusion : la capture voit ce qu'il y a derriere.
            apres = [a for a in achats_lus() if a.nom_item == "Piment cramoisi"
                     and a.prix_total == 3993]
            self.assertEqual(apres, [], "la fenetre masquee a encore ete lue")
        finally:
            piege.destroy()


class TestLancementSansConsole(unittest.TestCase):
    """La console affichait le journal, relu par l'OCR : jamais de console."""

    def test_le_manifest_lance_le_moteur_avec_pythonw(self):
        """Coût des plats etant un plugin, c'est son manifest qui porte le lancement."""
        import json

        plugin = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(plugin, "manifest.json"), encoding="utf-8") as fichier:
            manifest = json.load(fichier)
        lancement = manifest["launch"]
        self.assertEqual(lancement["command"], "pythonw")
        self.assertEqual(lancement["args"][0], "serveur.py")
        self.assertEqual(lancement["cwd"], "moteur")

    def test_journal_sans_stdout(self):
        """Sous pythonw, sys.stdout vaut None : les logs ne doivent pas planter."""
        import logging
        import sys
        import tempfile

        from journal import configurer_logs

        ancien = sys.stdout
        dossier = tempfile.mkdtemp()
        try:
            sys.stdout = None
            configurer_logs({"log": {"niveau": "INFO",
                                     "fichier": os.path.join(dossier, "t.log")}})
            logging.getLogger("test").info("message sous pythonw")
        finally:
            sys.stdout = ancien
            for gestionnaire in logging.getLogger().handlers[:]:
                gestionnaire.close()
                logging.getLogger().removeHandler(gestionnaire)


if __name__ == "__main__":
    unittest.main(verbosity=2)
