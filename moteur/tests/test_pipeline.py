"""Tests du pipeline : politique de file et saut des images identiques.

Lancement (depuis tools/cout-plats) : python -m unittest tests.test_pipeline -v
"""
from __future__ import annotations

import queue
import unittest

import config as module_config
from ocr import Capture, PipelineOCR


class _PipelineFactice(PipelineOCR):
    """Meme logique de file, sans capture d'ecran ni Tesseract."""

    def __init__(self, maxsize: int) -> None:
        self.file = queue.Queue(maxsize=maxsize)
        self.captures_perdues = 0


class TestFile(unittest.TestCase):
    def test_jette_la_plus_ancienne_quand_pleine(self):
        pipeline = _PipelineFactice(maxsize=3)
        for indice in range(5):
            pipeline._empiler(pipeline.file, Capture(float(indice), "image%d" % indice))
        restantes = []
        while not pipeline.file.empty():
            restantes.append(pipeline.file.get_nowait().image)
        # On garde les 3 plus recentes, pas les 3 premieres.
        self.assertEqual(restantes, ["image2", "image3", "image4"])
        self.assertEqual(pipeline.captures_perdues, 2)

    def test_file_non_pleine_ne_jette_rien(self):
        pipeline = _PipelineFactice(maxsize=4)
        for indice in range(3):
            pipeline._empiler(pipeline.file, Capture(float(indice), "image%d" % indice))
        self.assertEqual(pipeline.file.qsize(), 3)
        self.assertEqual(pipeline.captures_perdues, 0)


class TestFileDouble(unittest.TestCase):
    """Zone rapide + zone de securite : deux files, un seul compteur global."""

    def _pipeline(self):
        pipeline = _PipelineFactice(maxsize=3)
        pipeline.file_securite = queue.Queue(maxsize=1)
        return pipeline

    def test_taille_queue_additionne_les_deux_files(self):
        pipeline = self._pipeline()
        pipeline._empiler(pipeline.file, Capture(1.0, "a"))
        pipeline._empiler(pipeline.file_securite, Capture(2.0, "b"))
        self.assertEqual(pipeline.taille_queue, 2)

    def test_vider_jette_les_deux_files(self):
        pipeline = self._pipeline()
        pipeline._empiler(pipeline.file, Capture(1.0, "a"))
        pipeline._empiler(pipeline.file_securite, Capture(2.0, "b"))
        self.assertEqual(pipeline.vider(), 2)
        self.assertEqual(pipeline.taille_queue, 0)


class TestPipelineZoneRapide(unittest.TestCase):
    """capture.zone_rapide (optionnelle) fait de capture.zone un filet de securite."""

    def _configuration(self, avec_zone_rapide: bool):
        configuration = module_config.charger()
        cap = configuration["capture"]
        if avec_zone_rapide:
            zone = cap["zone"]
            # Sous-rectangle de la zone existante : garanti sur le meme ecran.
            cap["zone_rapide"] = {
                "left": zone["left"], "top": zone["top"],
                "width": min(80, zone["width"]), "height": min(40, zone["height"]),
            }
        else:
            # La config reelle (config.json) peut deja avoir zone_rapide : ce
            # test verifie le cas ou elle est absente, donc on l'efface.
            cap["zone_rapide"] = None
        return configuration

    def test_sans_zone_rapide_une_seule_zone_comme_avant(self):
        configuration = self._configuration(avec_zone_rapide=False)
        pipeline = PipelineOCR(configuration, lambda texte: None)
        self.assertIsNone(pipeline.boite_securite)
        self.assertEqual(pipeline.boite["width"], configuration["capture"]["zone"]["width"])

    def test_avec_zone_rapide_la_zone_habituelle_devient_le_filet_de_securite(self):
        configuration = self._configuration(avec_zone_rapide=True)
        pipeline = PipelineOCR(configuration, lambda texte: None)
        self.assertIsNotNone(pipeline.boite_securite)
        self.assertEqual(pipeline.boite["width"], configuration["capture"]["zone_rapide"]["width"])
        self.assertEqual(pipeline.boite_securite["width"], configuration["capture"]["zone"]["width"])


class TestConfigTesseract(unittest.TestCase):
    def test_pas_de_guillemet_ni_backslash_dans_la_config(self):
        """pytesseract decoupe sur les espaces et garde les guillemets (Windows)."""
        from ocr import construire_config_tesseract

        configuration = module_config.charger()
        options = construire_config_tesseract(configuration["ocr"])
        self.assertNotIn('"', options)
        self.assertNotIn("\\", options)
        # La whitelist passe par un fichier, jamais en ligne de commande.
        self.assertNotIn("tessedit_char_whitelist", options)
        self.assertIn("--psm", options)


if __name__ == "__main__":
    unittest.main(verbosity=2)
