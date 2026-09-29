"""Test du backend du hub : vrai serveur WebSocket, session sans OCR.

Lancement (depuis tools/cout-plats) : python -m unittest tests.test_serveur -v
"""
from __future__ import annotations

import asyncio
import copy
import json
import socket
import unittest
import urllib.request

import config as module_config
from catalogue import Ingredient, Recette
from moteur import Moteur
from parsing import Achat
from serveur import Serveur, servir
from tracker import Session


def _port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServeur(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from unittest import mock

        # Jamais le vrai config.json, ni la config par defaut partagee.
        ecriture = mock.patch("serveur.enregistrer_dans_config")
        ecriture.start()
        self.addCleanup(ecriture.stop)
        configuration = copy.deepcopy(module_config.CONFIG_DEFAUT)
        self.moteur = Moteur(configuration)
        self.moteur.session = Session([
            Recette(1, "Salade de fruits", [Ingredient(1, "Gelee Myrtille", 4),
                                  Ingredient(2, "Gelee Citron", 2)]),
        ])
        self.serveur = Serveur(configuration, self.moteur)
        self.port = _port_libre()
        self.tache = asyncio.create_task(servir(self.serveur, "127.0.0.1", self.port))
        for _ in range(50):
            try:
                _, sonde = await asyncio.open_connection("127.0.0.1", self.port)
                sonde.close()
                await sonde.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)

    async def asyncTearDown(self):
        self.tache.cancel()
        try:
            await self.tache
        except asyncio.CancelledError:
            pass

    async def _connecter(self):
        from websockets.asyncio.client import connect
        return await connect("ws://127.0.0.1:%d/ws" % self.port)

    @staticmethod
    async def _attendre(ws, predicat, delai=3.0):
        async def boucle():
            while True:
                message = json.loads(await ws.recv())
                if predicat(message):
                    return message
        return await asyncio.wait_for(boucle(), delai)

    async def test_health(self):
        url = "http://127.0.0.1:%d/health" % self.port
        reponse = await asyncio.to_thread(urllib.request.urlopen, url)
        self.assertEqual(reponse.status, 200)

    async def test_etat_complet_a_la_connexion(self):
        async with await self._connecter() as ws:
            types = set()
            for _ in range(3):
                message = json.loads(await asyncio.wait_for(ws.recv(), 3))
                types.add(message["type"])
                if message["type"] == "session_state":
                    recette = message["session"]["recettes"][0]
                    self.assertEqual(recette["nom"], "Salade de fruits")
                    self.assertEqual(recette["ingredients"][0]["quantite_requise"], 4)
            self.assertEqual(types, {"session_state", "queue_status", "backend_status"})

    async def test_increment_fige_et_repond(self):
        self.moteur.session.traiter_achat(Achat("14:30", 2, "Gelee Myrtille", 200))
        async with await self._connecter() as ws:
            await ws.send(json.dumps({"type": "manual_increment", "id": 7,
                                      "ingredient": "Gelee Myrtille", "quantite": 2}))
            resultat = await self._attendre(ws, lambda m: m["type"] == "command_result")
            self.assertEqual((resultat["id"], resultat["ok"]), (7, True))
            fige = await self._attendre(ws, lambda m: m["type"] == "recipe_frozen")
            self.assertEqual((fige["recette"], fige["ingredient"], fige["prix_fige"]),
                             ("Salade de fruits", "Gelee Myrtille", 100.0))
            etat = self.moteur.session.etat()[0].ingredients[0]
            self.assertEqual(etat.quantite_cumulee, 4)

    async def test_commande_invalide_ou_inconnue(self):
        async with await self._connecter() as ws:
            await ws.send(json.dumps({"type": "manual_increment", "id": 1,
                                      "ingredient": "Inconnu", "quantite": 1}))
            await ws.send(json.dumps({"type": "n_existe_pas", "id": 2}))
            await ws.send("pas du json")
            recus = {}
            while len(recus) < 2:
                m = await self._attendre(ws, lambda m: m["type"] == "command_result")
                recus[m["id"]] = m
            self.assertFalse(recus[1]["ok"])
            self.assertIn("hors session", recus[1]["message"])
            self.assertFalse(recus[2]["ok"])

    async def test_stock_dans_la_liste_d_achats(self):
        async with await self._connecter() as ws:
            await ws.send(json.dumps({"type": "set_stock", "id": 9,
                                      "ingredient": "Gelee Myrtille", "quantite": 3}))
            resultat = await self._attendre(ws, lambda m: m["type"] == "command_result")
            self.assertTrue(resultat["ok"], resultat)
            etat = await self._attendre(ws, lambda m: m["type"] == "session_state" and any(
                l["stock"] == 3 for l in m["session"]["liste_courses"]))
            ingredient = etat["session"]["recettes"][0]["ingredients"][0]
            self.assertEqual(ingredient["quantite_stock"], 3)

    async def test_catalogue_ajoute_sans_reset_puis_retire(self):
        from unittest import mock

        self.moteur.session.correction_manuelle("Gelee Myrtille", 4, 400)
        cape = Recette(744, "Bouillon de volaille", [Ingredient(384, "Os de volaille", 3)])
        with mock.patch("catalogue.ClientCatalogue.recettes_par_ids",
                        return_value=([cape], [(1, "Aucune recette pour Pain perdu")])):
            async with await self._connecter() as ws:
                await ws.send(json.dumps({"type": "add_items", "id": 5, "items": [
                    {"id": 744, "quantite": 2}, {"id": 1}]}))
                resultat = await self._attendre(ws, lambda m: m["type"] == "command_result")
                self.assertTrue(resultat["ok"], resultat)
                self.assertIn("Bouillon de volaille x2", resultat["message"])
                self.assertIn("Sans recette", resultat["message"])

                etat = await self._attendre(ws, lambda m: m["type"] == "session_state"
                                            and len(m["session"]["recettes"]) == 2)
                noms = [r["nom"] for r in etat["session"]["recettes"]]
                self.assertEqual(noms, ["Salade de fruits", "Bouillon de volaille"])
                self.assertEqual(etat["session"]["recettes"][1]["nb_plats"], 2)
                self.assertEqual(self.moteur.session.quantite_cumulee["Gelee Myrtille"], 4)

                await ws.send(json.dumps({"type": "add_items", "id": 6, "items": [{"id": 744}]}))
                doublon = await self._attendre(ws, lambda m: m["type"] == "command_result"
                                               and m["id"] == 6)
                self.assertFalse(doublon["ok"])

                await ws.send(json.dumps({"type": "remove_recipe", "id": 7,
                                          "recette": "Bouillon de volaille"}))
                retrait = await self._attendre(ws, lambda m: m["type"] == "command_result"
                                               and m["id"] == 7)
                self.assertTrue(retrait["ok"], retrait)
        self.assertEqual([r.nom for r in self.moteur.session.recettes], ["Salade de fruits"])

    def test_reset_consomme_et_change_la_quantite(self):
        session = self.moteur.session  # Salade de fruits : 4 Myrtille, 2 Citron
        session.traiter_achat(Achat("14:30", 6, "Gelee Myrtille", 600))
        message = self.serveur.cmd_cuisine({"recette": "Salade de fruits", "nombre": 3})
        self.assertIn("x3", message)
        # "achete" repart a zero (surplus de 2 rejoint le stock), le prix
        # moyen (100/u) reste accessible comme reference.
        self.assertEqual(session.quantite_cumulee["Gelee Myrtille"], 0)
        self.assertEqual(session.stock["Gelee Myrtille"], 2)
        self.assertAlmostEqual(session.prix_moyen("Gelee Myrtille"), 100.0)
        self.assertEqual(session.nb_plats["Salade de fruits"], 3)

    def test_plats_memorises_puis_restaures(self):
        from unittest import mock

        session = self.moteur.session
        session.definir_nb_plats("Salade de fruits", 25)
        session.definir_prix_vente("Salade de fruits", 90000)
        self.serveur.cmd_memoriser_plat({"recette": "Salade de fruits"})
        self.serveur.cmd_memoriser_plat({"recette": "Salade de fruits"})  # pas de doublon
        self.assertEqual(self.serveur.plats_memorises, [
            {"id": 1, "nom": "Salade de fruits", "quantite": 25, "prix_vente": 90000}])
        self.assertEqual(self.serveur.statut_backend()["plats_memorises"][0]["quantite"], 25)

        session.retirer_recette("Salade de fruits")
        salade = Recette(1, "Salade de fruits", [Ingredient(1, "Gelee Myrtille", 4)])
        with mock.patch("catalogue.ClientCatalogue.recettes_par_ids", return_value=([salade], [])):
            self.serveur.cmd_restaurer_plats({"ids": [1]})
        self.assertEqual(session.nb_plats["Salade de fruits"], 25)
        self.assertEqual(session.prix_vente["Salade de fruits"], 90000)
        self.assertEqual(self.serveur.plats_memorises, [])

        self.serveur.cmd_memoriser_plat({"recette": "Salade de fruits"})
        self.serveur.cmd_oublier_plat({"id": 1})
        self.assertEqual(self.serveur.plats_memorises, [])

    def test_vider_la_queue_ocr(self):
        from unittest import mock

        from serveur import ErreurCommande

        with self.assertRaises(ErreurCommande):
            self.serveur.cmd_vider_queue({})
        self.moteur.pipeline = mock.Mock(**{"vider.return_value": 4})
        try:
            self.assertIn("4 capture", self.serveur.cmd_vider_queue({}))
        finally:
            self.moteur.pipeline = None

    async def test_confirmation_d_un_achat_en_attente(self):
        session = self.moteur.session
        session.seuil_auto = 101  # tout match passe par la confirmation
        session.traiter_achat(Achat("14:31", 1, "Gelee Myrtille", 90))
        attente = session.achats_en_attente()
        self.assertEqual(len(attente), 1)
        async with await self._connecter() as ws:
            await ws.send(json.dumps({"type": "confirm_purchase", "id": 3,
                                      "identifiant": attente[0].identifiant}))
            resultat = await self._attendre(ws, lambda m: m["type"] == "command_result")
            self.assertTrue(resultat["ok"], resultat)
            etat = await self._attendre(
                ws, lambda m: m["type"] == "session_state" and not m["session"]["en_attente"])
            self.assertEqual(etat["session"]["nb_achats"], 1)


class TestEnregistrementConfig(unittest.TestCase):
    def test_ne_fige_pas_les_valeurs_resolues(self):
        """Seules les cles modifiees changent : tessdata reste relatif."""
        import os
        import tempfile

        from serveur import enregistrer_dans_config

        with tempfile.TemporaryDirectory() as dossier:
            chemin = os.path.join(dossier, "config.json")
            with open(chemin, "w", encoding="utf-8-sig") as f:
                json.dump({"ocr": {"tessdata_dir": "tessdata"},
                           "session": {"max_items": 12}}, f)
            enregistrer_dans_config({"session": {"items_par_defaut": ["Salade de fruits"]}}, chemin)
            with open(chemin, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {
                    "ocr": {"tessdata_dir": "tessdata"},
                    "session": {"max_items": 12, "items_par_defaut": ["Salade de fruits"]},
                })


if __name__ == "__main__":
    unittest.main()
