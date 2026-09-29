"""Tests du selecteur de zone : detection de l'ecran, coordonnees absolues.

Lancement (depuis tools/cout-plats) : python -m unittest tests.test_zone -v
"""
from __future__ import annotations

import unittest

from selecteur_zone import monitor_contenant

# Convention mss : index 0 = bureau virtuel, puis un dict par ecran.
# Cas courant : un ecran secondaire a GAUCHE du principal -> left negatif.
ECRANS = [
    {"left": -1920, "top": 0, "width": 3840, "height": 1080},  # bureau virtuel
    {"left": 0, "top": 0, "width": 1920, "height": 1080},      # principal
    {"left": -1920, "top": 0, "width": 1920, "height": 1080},  # secondaire
]


class TestMonitorContenant(unittest.TestCase):
    def test_zone_sur_ecran_principal(self):
        zone = {"left": 20, "top": 780, "width": 900, "height": 260}
        self.assertEqual(monitor_contenant(zone, ECRANS), 1)

    def test_zone_sur_ecran_secondaire_en_negatif(self):
        zone = {"left": -1800, "top": 700, "width": 900, "height": 260}
        self.assertEqual(monitor_contenant(zone, ECRANS), 2)

    def test_zone_a_cheval_suit_son_centre(self):
        # Centre a x = -10 -> ecran secondaire.
        zone = {"left": -210, "top": 100, "width": 400, "height": 100}
        self.assertEqual(monitor_contenant(zone, ECRANS), 2)

    def test_zone_hors_de_tout_ecran_retombe_sur_le_principal(self):
        zone = {"left": 9000, "top": 9000, "width": 100, "height": 100}
        self.assertEqual(monitor_contenant(zone, ECRANS), 1)

    def test_ecran_unique(self):
        ecrans = [
            {"left": 0, "top": 0, "width": 2560, "height": 1440},
            {"left": 0, "top": 0, "width": 2560, "height": 1440},
        ]
        zone = {"left": 100, "top": 1200, "width": 800, "height": 200}
        self.assertEqual(monitor_contenant(zone, ecrans), 1)


class TestZoneReelle(unittest.TestCase):
    def test_bureau_virtuel_englobe_les_ecrans(self):
        from selecteur_zone import bureau_virtuel, ecrans as lister_ecrans

        liste = lister_ecrans()
        bureau = bureau_virtuel()
        for ecran in liste[1:]:
            self.assertGreaterEqual(ecran["left"], bureau["left"])
            self.assertGreaterEqual(ecran["top"], bureau["top"])
            self.assertLessEqual(ecran["left"] + ecran["width"],
                                 bureau["left"] + bureau["width"])
            self.assertLessEqual(ecran["top"] + ecran["height"],
                                 bureau["top"] + bureau["height"])

    def test_zone_configuree_est_capturable(self):
        """La zone de config.json doit etre reellement saisissable par mss."""
        import config as module_config
        from ocr import verifier_zone

        configuration = module_config.charger()
        boite = verifier_zone(configuration["capture"])
        self.assertEqual(boite["width"], configuration["capture"]["zone"]["width"])


class TestSelecteurSimule(unittest.TestCase):
    """Glisser-deposer simule : verifie le passage en coordonnees absolues."""

    @classmethod
    def setUpClass(cls):
        import tkinter as tk

        try:
            cls.racine = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - machine sans affichage
            raise unittest.SkipTest("Pas d'affichage : %s" % exc)
        cls.racine.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.racine.destroy()

    def _selecteur(self):
        from selecteur_zone import SelecteurZone

        selecteur = SelecteurZone(self.racine)
        selecteur.update()
        return selecteur

    @staticmethod
    def _evenement(x, y):
        import types

        return types.SimpleNamespace(x=x, y=y)

    def test_glisser_donne_des_coordonnees_absolues(self):
        from selecteur_zone import bureau_virtuel

        bureau = bureau_virtuel()
        selecteur = self._selecteur()
        selecteur._debut(self._evenement(100, 200))
        selecteur._glisse(self._evenement(500, 400))
        selecteur._fin(self._evenement(1000, 460))
        self.assertEqual(selecteur.zone, {
            "left": 100 + bureau["left"], "top": 200 + bureau["top"],
            "width": 900, "height": 260,
        })

    def test_glisser_en_sens_inverse(self):
        """Du coin bas-droit vers le haut-gauche : meme rectangle."""
        selecteur = self._selecteur()
        selecteur._debut(self._evenement(1000, 460))
        selecteur._fin(self._evenement(100, 200))
        self.assertEqual(selecteur.zone["width"], 900)
        self.assertEqual(selecteur.zone["height"], 260)

    def test_clic_parasite_ignore(self):
        """Un clic sans glisser ne doit pas definir une zone minuscule."""
        selecteur = self._selecteur()
        selecteur._debut(self._evenement(10, 10))
        selecteur._fin(self._evenement(15, 12))
        self.assertIsNone(selecteur.zone)
        selecteur.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
