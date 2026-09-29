"""Tests de la logique critique : parsing, deduplication positionnelle, figeage.

Lancement (depuis tools/cout-plats) : python -m unittest tests.test_logique -v
"""
from __future__ import annotations

import unittest

from catalogue import Ingredient, Recette
from parsing import Achat, DedupeurDeFlux, decouper_messages, parser_message
from tracker import STATUT_A_CONFIRMER, STATUT_COMPTE, STATUT_REJETE, Session


class TestParsing(unittest.TestCase):
    def test_achat_simple(self):
        achat = parser_message("[14:32] 10 x [Epices de Cayenne] (12000 euros)")
        self.assertIsNotNone(achat)
        self.assertEqual(
            (achat.heure, achat.quantite, achat.nom_item, achat.prix_total),
            ("14:32", 10, "Epices de Cayenne", 12000),
        )

    def test_nom_coupe_sur_deux_lignes(self):
        texte = "[14:32] 10 x [Tofu soyeux\naffine] (900 euros)"
        messages = decouper_messages(texte)
        self.assertEqual(len(messages), 1)
        achat = parser_message(messages[0])
        self.assertEqual(achat.nom_item, "Tofu soyeux affine")
        self.assertEqual(achat.quantite, 10)

    def test_quantite_hors_lots_grossiste_ecartee(self):
        # Regression : "[21:521 1 x" lu "71 x" comptait +71 au lieu de +1.
        for quantite in (71, 11, 2, 240):
            self.assertIsNone(parser_message(
                "[21:52] %d x [Poivron rouge] (132 euros)" % quantite))
        for quantite in (1, 10, 100, 1000):
            self.assertEqual(parser_message(
                "[21:52] %d x [Poivron rouge] (132 euros)" % quantite).quantite, quantite)

    def test_message_vente_ignore(self):
        self.assertIsNone(parser_message("[14:33] Banque : + 15000 Euros (vente)"))

    def test_decoupe_par_horodatage_pas_par_ligne(self):
        texte = (
            "[14:30] 1 x [Bois de\nFrene] (100 euros)\n"
            "[14:31] 10 x [Fleur de courgette] (250 euros)\n"
            "[14:31] Banque : + 500 Euros (vente de Salade de fruits)"
        )
        messages = decouper_messages(texte)
        self.assertEqual(len(messages), 3)
        achats = [parser_message(m) for m in messages]
        self.assertEqual([a is not None for a in achats], [True, True, False])


class TestChatReel(unittest.TestCase):
    """Lignes relevees dans le vrai fil de commandes (capture d'ecran).

    Le logiciel du fournisseur separe les milliers par une espace : tout achat a 1000 euros ou plus
    etait silencieusement ignore.
    """

    CHAT = (
        "Sel royal]).\n"  # ligne du haut coupee par le cadrage
        "[04:29] Vous avez perdu 20 euros.\n"
        "[04:29] 1 x [Fragment d'Épée Reptilienne] (1 067 euros)\n"
        "[04:29] 1 x [Fragment d'Épée Reptilienne] (1 088 euros)\n"
        "[04:30] 1 x [Tete de veau] (195 euros)\n"
        "[04:31] 1 x [Fleur de sel royale] (894 euros)\n"
        "[04:31] 1 x [Piment cramoisi] (2 299 euros)\n"
        "[04:31] 1 x [Piment cramoisi] (2 299 euros)\n"
        "[04:31] 1 x [Ris de veau] (1 198 euros)\n"
        "[04:31] 1 x [Ris de veau] (1 798 euros)\n"
    )

    def _achats(self):
        return [parser_message(m) for m in decouper_messages(self.CHAT)]

    def test_les_prix_a_separateur_de_milliers_sont_lus(self):
        achats = [a for a in self._achats() if a]
        self.assertEqual(len(achats), 8)
        self.assertEqual(achats[0].prix_total, 1067)
        self.assertEqual(achats[4].prix_total, 2299)
        self.assertEqual(achats[7].prix_total, 1798)

    def test_message_systeme_non_compte(self):
        messages = decouper_messages(self.CHAT)
        self.assertIsNone(parser_message(messages[0]))  # "Vous avez perdu 20 euros."

    def test_ligne_du_haut_coupee_ignoree(self):
        # Sans horodatage, ce n'est pas un message : il ne doit pas etre decoupe.
        self.assertNotIn("Sel royal", decouper_messages(self.CHAT)[0])

    def test_separateurs_exotiques(self):
        """Espace insecable, espace fine, point ou virgule selon l'OCR."""
        for brut, attendu in [
            ("(1 067 euros)", 1067),
            ("(1 067 euros)", 1067),
            ("(1.067 euros)", 1067),
            ("(1,067 euros)", 1067),
            ("(12 345 678 euros)", 12345678),
        ]:
            achat = parser_message("[04:29] 1 x [Piment cramoisi] %s" % brut)
            self.assertIsNotNone(achat, brut)
            self.assertEqual(achat.prix_total, attendu, brut)


class TestDedupePositionnelle(unittest.TestCase):
    def test_achats_identiques_consecutifs_tous_comptes(self):
        """Trois lignes rigoureusement identiques = trois transactions."""
        dedupeur = DedupeurDeFlux()
        ligne = "[14:32] 1 x [Bois de Frene] (100 euros)"
        self.assertEqual(len(dedupeur.nouveaux([ligne])), 1)
        self.assertEqual(len(dedupeur.nouveaux([ligne, ligne])), 1)
        self.assertEqual(len(dedupeur.nouveaux([ligne, ligne, ligne])), 1)

    def test_capture_inchangee_ne_recompte_rien(self):
        dedupeur = DedupeurDeFlux()
        capture = ["[14:30] 1 x [A] (10 euros)", "[14:31] 2 x [B] (20 euros)"]
        self.assertEqual(len(dedupeur.nouveaux(capture)), 2)
        self.assertEqual(dedupeur.nouveaux(capture), [])
        self.assertEqual(dedupeur.nouveaux(capture), [])

    def test_defilement_du_chat(self):
        dedupeur = DedupeurDeFlux()
        dedupeur.nouveaux(["m1", "m2", "m3"])
        nouveaux = dedupeur.nouveaux(["m2", "m3", "m4", "m5"])
        self.assertEqual(nouveaux, ["m4", "m5"])

    def test_bruit_ocr_leger_non_recompte(self):
        dedupeur = DedupeurDeFlux()
        dedupeur.nouveaux(["[14:30] 12 x [Fleur de courgette] (2400 euros)"])
        nouveaux = dedupeur.nouveaux(["[14:30] 12 x [Fleur de courgette] (2400 eurosi)"])
        self.assertEqual(nouveaux, [])

    def test_une_ligne_bruitee_au_milieu_ne_casse_pas_l_alignement(self):
        """Cas observe en service : sinon, toute la fenetre etait recomptee."""
        dedupeur = DedupeurDeFlux()
        fenetre = ["[04:3%d] 1 x [Piment cramoisi] (2 299 euros)" % i for i in range(8)]
        self.assertEqual(len(dedupeur.nouveaux(fenetre)), 8)
        bruitee = list(fenetre)
        bruitee[3] = "[04:33] 1 x [Ga1et cram0is1] (2 z99 kannas)"
        self.assertEqual(dedupeur.nouveaux(bruitee), [])

    def test_perte_d_alignement_ne_recompte_rien(self):
        """Le bug observe en service : un cumul qui montait a chaque hoquet OCR."""
        dedupeur = DedupeurDeFlux()
        connus = ["[04:31] 1 x [Fleur de sel] (894 euros)"] * 8
        self.assertEqual(len(dedupeur.nouveaux(connus)), 8)

        # Fenetre sans aucun rapport (et pas plus recente) : on se
        # resynchronise sans rien compter.
        etrangers = ["[04:2%d] 1 x [Autre chose] (10 euros)" % i for i in range(6)]
        self.assertEqual(dedupeur.nouveaux(etrangers), [])
        self.assertEqual(dedupeur.resynchronisations, 1)

        # Et on repart proprement depuis cette nouvelle fenetre.
        suite = etrangers + ["[04:26] 1 x [Autre chose] (10 euros)"]
        self.assertEqual(len(dedupeur.nouveaux(suite)), 1)
        self.assertEqual(dedupeur.resynchronisations, 1)

    def test_resync_compte_les_messages_posterieurs_a_tout_l_historique(self):
        """Bug reel : 12 x Asperge verte achetes, 11 comptes.

        La premiere ligne de la rafale est arrivee seule dans une capture sans
        aucun recouvrement avec le chat connu (tout plus ancien, 23:22-23:24).
        La resynchronisation l'ignorait. Or une minute strictement posterieure
        a tout ce qu'on a lu ne peut pas avoir ete comptee.
        """
        dedupeur = DedupeurDeFlux()
        anciens = ["[23:22] Vous etes desormais reference dans toutes les listes d'artisans",
                   "[23:24] 1 x [Noisette] (72 euros)",
                   "[23:24] 1 x [Noisette] (72 euros)"]
        dedupeur.nouveaux(anciens)
        pic = "[23:28] 10 x [Asperge verte] (2 799 euros)"

        comptes = len(dedupeur.nouveaux([pic + " [ i e"]))  # la capture du log
        self.assertEqual(comptes, 1)
        self.assertEqual(dedupeur.resynchronisations, 1)
        for visibles in range(2, 13):  # la rafale continue de s'afficher
            comptes += len(dedupeur.nouveaux([pic] * visibles))
        self.assertEqual(comptes, 12)

    def test_resync_ne_compte_pas_la_meme_minute(self):
        """Meme minute que le dernier message connu : peut-etre deja compte."""
        dedupeur = DedupeurDeFlux()
        dedupeur.nouveaux(["[23:24] 1 x [Noisette] (72 euros)"] * 3)
        etrangers = ["[23:24] 1 x [Ble] (10 euros)", "[23:24] 2 x [Orge] (20 euros)"]
        self.assertEqual(dedupeur.nouveaux(etrangers), [])
        self.assertEqual(dedupeur.resynchronisations, 1)

    def test_resync_ne_garde_que_la_fin_posterieure(self):
        """Ancien message puis nouveaux : seuls les nouveaux, en fin de fenetre."""
        dedupeur = DedupeurDeFlux()
        dedupeur.nouveaux(["[10:05] 1 x [A] (10 euros)"] * 3)
        fenetre = ["[10:01] 1 x [Vieux] (5 euros)",
                   "[10:06] 1 x [B] (10 euros)",
                   "[10:07] 1 x [C] (10 euros)"]
        self.assertEqual(dedupeur.nouveaux(fenetre), fenetre[1:])

    def test_remonter_le_chat_puis_revenir_ne_recompte_rien(self):
        """Resync vers du vieux contenu puis retour : rien n'est recompte."""
        dedupeur = DedupeurDeFlux()
        courant = ["[10:05] 1 x [A] (10 euros)", "[10:06] 1 x [B] (10 euros)",
                   "[10:07] 1 x [C] (10 euros)"]
        self.assertEqual(len(dedupeur.nouveaux(courant)), 3)
        vieux = ["[09:0%d] 1 x [Vieux] (5 euros)" % i for i in range(5)]
        self.assertEqual(dedupeur.nouveaux(vieux), [])
        self.assertEqual(dedupeur.nouveaux(courant), [])
        # Un vrai nouvel achat, lui, passe.
        nouvel_achat = "[10:08] 1 x [Tofu soyeux] (10 euros)"
        self.assertEqual(dedupeur.nouveaux(courant + [nouvel_achat]), [nouvel_achat])

    def test_resync_apres_minuit_ne_compte_rien(self):
        """00:01 < 23:59 : ambigu, on reste prudent."""
        dedupeur = DedupeurDeFlux()
        dedupeur.nouveaux(["[23:59] 1 x [A] (10 euros)"] * 3)
        self.assertEqual(dedupeur.nouveaux(["[00:01] 1 x [B] (10 euros)"]), [])

    def test_chat_plus_grand_que_l_historique(self):
        """Bug reel : 62 messages visibles pour 60 memorises, resync a chaque capture."""
        dedupeur = DedupeurDeFlux()
        items = ["Truffe noire", "Mesclun de saison", "Brins de ciboulette",
                 "Pinces de homard", "Ailes de raie", "Ébonite"]
        achat = lambda i: "[02:%02d] %d x [%s] (%d euros)" % (
            i // 4, 1 + i % 3 * 9, items[i % len(items)], 19 * (i + 1))
        fenetre = [achat(i) for i in range(62)]
        self.assertEqual(len(dedupeur.nouveaux(fenetre)), 62)
        for i in range(62, 80):  # le chat defile d'une ligne par achat
            nouvel = achat(i)
            fenetre = fenetre[1:] + [nouvel]
            self.assertEqual(dedupeur.nouveaux(fenetre), [nouvel])
        self.assertEqual(dedupeur.resynchronisations, 0)

    def test_ancre_sur_le_dernier_message_connu(self):
        """Bug reel (Champignon de Paris) : petite fenetre, une ligne mal lue."""
        dedupeur = DedupeurDeFlux()
        dedupeur.nouveaux(["[01:38] 10 x [Filet de sanglier] (3 993 euros)",
                           "[01:40] 1 x [Ébonite] (6 277 euros)"])
        champignon = "[01:40] 1 x [Champignon de Paris] (754 euros)"
        capture = ["[01:38] 10 x [Dcfcnsc du Sang1icr] (3 898 euros)",
                   "[01:40]1 x [Èbonito] (6 277 euros)",
                   champignon]
        self.assertEqual(dedupeur.nouveaux(capture), [champignon])
        self.assertEqual(dedupeur.resynchronisations, 0)

    def test_pas_d_ancre_si_le_dernier_message_est_repete(self):
        """Rafale identique : impossible de savoir lequel etait le dernier."""
        dedupeur = DedupeurDeFlux()
        ligne = "[04:31] 1 x [Piment cramoisi] (2 299 euros)"
        dedupeur.nouveaux(["[04:30] 1 x [Tofu soyeux] (10 euros)", ligne])
        capture = ["[04:30] 1 x [Pl@m# d# T0f#] (1O kanna)", ligne, ligne, ligne]
        self.assertEqual(dedupeur.nouveaux(capture), [])
        self.assertEqual(dedupeur.resynchronisations, 1)

    def test_resync_signale_les_ecartes_de_la_derniere_minute(self):
        dedupeur = DedupeurDeFlux()
        dedupeur.nouveaux(["[23:24] 1 x [Noisette] (72 euros)"] * 3)
        etrangers = ["[23:20] 1 x [Vieux] (5 euros)", "[23:24] 1 x [Ble] (10 euros)"]
        self.assertEqual(dedupeur.nouveaux(etrangers), [])
        self.assertEqual(dedupeur.ecartes_douteux, ["[23:24] 1 x [Ble] (10 euros)"])
        self.assertEqual(dedupeur.nouveaux(etrangers), [])
        self.assertEqual(dedupeur.ecartes_douteux, [])  # alignee : plus rien de douteux

    def test_chat_fige_ne_gonfle_jamais_les_compteurs(self):
        """Chat immobile relu 30 fois : aucun achat supplementaire."""
        dedupeur = DedupeurDeFlux()
        fenetre = ["[04:31] 1 x [Piment cramoisi] (2 299 euros)"] * 10
        self.assertEqual(len(dedupeur.nouveaux(fenetre)), 10)
        for _ in range(30):
            self.assertEqual(dedupeur.nouveaux(fenetre), [])
        self.assertEqual(dedupeur.resynchronisations, 0)


def _session():
    # Deux recettes partageant "Bois de Frene" avec des quantites requises differentes.
    recette_a = Recette(1, "Blanquette de veau", [
        Ingredient(303, "Bois de Frene", 3),
        Ingredient(16512, "Poivre Sichuan", 2),
    ])
    recette_b = Recette(2, "Tarte aux pommes", [
        Ingredient(303, "Bois de Frene", 10),
    ])
    return Session([recette_a, recette_b], seuil_auto=90, seuil_confirmation=70)


class TestFigeage(unittest.TestCase):
    def test_figeage_independant_par_recette(self):
        session = _session()
        # 3 unites a 100 k/u -> recette A (requis 3) se fige, pas B (requis 10).
        session.correction_manuelle("Bois de Frene", 3, 300)
        etats = {e.nom: e for e in session.etat()}
        self.assertAlmostEqual(
            etats["Blanquette de veau"].ingredients[0].prix_fige, 100.0)
        self.assertIsNone(etats["Tarte aux pommes"].ingredients[0].prix_fige)

        # 7 unites de plus a 200 k/u -> moyenne 170, B se fige a 170, A reste a 100.
        session.correction_manuelle("Bois de Frene", 7, 1400)
        etats = {e.nom: e for e in session.etat()}
        self.assertAlmostEqual(
            etats["Blanquette de veau"].ingredients[0].prix_fige, 100.0)
        self.assertAlmostEqual(
            etats["Tarte aux pommes"].ingredients[0].prix_fige, 170.0)

    def test_cout_total_quand_tout_est_fige(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 3, 300)      # fige A a 100
        session.correction_manuelle("Poivre Sichuan", 2, 1000)  # fige A a 500
        etats = {e.nom: e for e in session.etat()}
        # 3 x 100 + 2 x 500 = 1300
        self.assertAlmostEqual(etats["Blanquette de veau"].cout_total, 1300.0)
        self.assertIsNone(etats["Tarte aux pommes"].cout_total)

    def test_prix_moyen_continue_apres_figeage(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 3, 300)
        session.correction_manuelle("Bois de Frene", 1, 500)
        etats = {e.nom: e for e in session.etat()}
        ingredient = etats["Blanquette de veau"].ingredients[0]
        self.assertAlmostEqual(ingredient.prix_fige, 100.0)   # fige, inchange
        self.assertAlmostEqual(ingredient.prix_moyen, 200.0)  # moyenne courante

    def test_correction_sans_prix_ne_deforme_pas_la_moyenne(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 2, 400)  # 200 k/u
        session.correction_manuelle("Bois de Frene", 1)       # sans prix
        self.assertAlmostEqual(session.prix_moyen("Bois de Frene"), 200.0)


class TestNombreDePlats(unittest.TestCase):
    """Cuisiner N fois le meme item multiplie toutes les quantites requises."""

    def test_seuil_de_figeage_suit_le_nombre_de_plats(self):
        session = _session()
        session.definir_nb_plats("Blanquette de veau", 20)  # 3 bois -> 60 bois
        session.correction_manuelle("Bois de Frene", 3, 300)
        etats = {e.nom: e for e in session.etat()}
        self.assertEqual(etats["Blanquette de veau"].ingredients[0].quantite_requise, 60)
        self.assertIsNone(etats["Blanquette de veau"].ingredients[0].prix_fige)

        session.correction_manuelle("Bois de Frene", 57, 5700)  # 60 au total
        etats = {e.nom: e for e in session.etat()}
        self.assertIsNotNone(etats["Blanquette de veau"].ingredients[0].prix_fige)
        self.assertAlmostEqual(
            etats["Blanquette de veau"].ingredients[0].prix_fige, 100.0)

    def test_cout_total_et_cout_unitaire(self):
        session = _session()
        session.definir_nb_plats("Blanquette de veau", 20)
        session.correction_manuelle("Bois de Frene", 60, 6000)      # 100 k/u
        session.correction_manuelle("Poivre Sichuan", 40, 20000)  # 500 k/u
        etat = {e.nom: e for e in session.etat()}["Blanquette de veau"]
        # 60 x 100 + 40 x 500 = 26000 pour 20 plats, soit 1300 l'unite.
        self.assertAlmostEqual(etat.cout_total, 26000.0)
        self.assertAlmostEqual(etat.cout_unitaire, 1300.0)

    def test_augmenter_les_plats_defige_ce_qui_ne_suffit_plus(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 3, 300)  # fige a 1 plat
        self.assertIsNotNone(session.prix_fige["Blanquette de veau"].get("Bois de Frene"))
        session.definir_nb_plats("Blanquette de veau", 20)
        self.assertIsNone(session.prix_fige["Blanquette de veau"].get("Bois de Frene"))

    def test_les_autres_recettes_ne_sont_pas_touchees(self):
        session = _session()
        session.definir_nb_plats("Blanquette de veau", 10)
        etats = {e.nom: e for e in session.etat()}
        self.assertEqual(etats["Tarte aux pommes"].nb_plats, 1)
        self.assertEqual(etats["Tarte aux pommes"].ingredients[0].quantite_requise, 10)


class TestStock(unittest.TestCase):
    """Ressources deja possedees (chambre froide, reserve)."""

    def _session(self):
        recette = Recette(1, "Shako", [Ingredient(1, "Asperge verte", 20),
                                       Ingredient(2, "Laine", 1)])
        return Session([recette], seuil_auto=90, seuil_confirmation=70)

    def test_le_stock_compte_pour_le_requis_pas_pour_la_moyenne(self):
        """20 requis, 3 en banque, 17 achetes a 280 : cout 20 x 280."""
        session = self._session()
        self.assertTrue(session.definir_stock("Asperge verte", 3))
        session.correction_manuelle("Asperge verte", 16, 16 * 280)
        self.assertIsNone(session.prix_fige["Shako"].get("Asperge verte"))  # 19/20
        session.correction_manuelle("Asperge verte", 1, 280)
        self.assertAlmostEqual(session.prix_fige["Shako"]["Asperge verte"], 280.0)
        self.assertAlmostEqual(session.prix_moyen("Asperge verte"), 280.0)

        session.correction_manuelle("Laine", 1, 50)
        recette = session.etat()[0]
        self.assertAlmostEqual(recette.cout_total, 20 * 280 + 50)
        self.assertEqual(recette.ingredients[0].quantite_stock, 3)
        self.assertEqual(recette.ingredients[0].quantite_cumulee, 17)

    def test_liste_d_achats_avec_stock_et_prix_total(self):
        session = self._session()
        session.definir_stock("Asperge verte", 3)
        session.correction_manuelle("Asperge verte", 10, 10 * 300)
        ligne = {e["nom"]: e for e in session.liste_courses()}["Asperge verte"]
        self.assertEqual((ligne["besoin"], ligne["stock"], ligne["achete"], ligne["reste"]),
                         (20, 3, 10, 7))
        self.assertAlmostEqual(ligne["prix_moyen"], 300.0)
        self.assertAlmostEqual(ligne["prix_total"], 6000.0)  # 20 x 300
        laine = {e["nom"]: e for e in session.liste_courses()}["Laine"]
        self.assertIsNone(laine["prix_total"])  # rien achete : pas de prix

    def test_stock_seul_ne_fige_pas_sans_aucun_prix(self):
        session = self._session()
        session.definir_stock("Asperge verte", 25)
        self.assertIsNone(session.prix_fige["Shako"].get("Asperge verte"))
        session.correction_manuelle("Asperge verte", 1, 300)  # premier prix connu
        self.assertAlmostEqual(session.prix_fige["Shako"]["Asperge verte"], 300.0)

    def test_ajouter_du_stock_fige_et_en_retirer_defige(self):
        session = self._session()
        session.correction_manuelle("Asperge verte", 18, 18 * 250)
        self.assertIsNone(session.prix_fige["Shako"].get("Asperge verte"))
        session.definir_stock("Asperge verte", 2)
        self.assertAlmostEqual(session.prix_fige["Shako"]["Asperge verte"], 250.0)
        session.definir_stock("Asperge verte", 0)
        self.assertIsNone(session.prix_fige["Shako"].get("Asperge verte"))

    def test_stock_refuse_hors_session_ou_negatif(self):
        session = self._session()
        self.assertFalse(session.definir_stock("Inconnu", 3))
        self.assertFalse(session.definir_stock("Laine", -1))


class TestAjoutEtRetraitDeRecettes(unittest.TestCase):
    """Catalogue du hub : la session evolue sans perdre ses compteurs."""

    def test_ajout_conserve_les_compteurs_et_fige_si_deja_achete(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 10, 1000)  # 100 k/u
        nouvelle = Recette(3, "Baton de Frene", [Ingredient(303, "Bois de Frene", 5),
                                                 Ingredient(9, "Resine", 1)])
        self.assertEqual(session.ajouter_recettes([nouvelle], {"Baton de Frene": 2}),
                         ["Baton de Frene"])
        self.assertEqual(session.quantite_cumulee["Bois de Frene"], 10)
        self.assertEqual(session.nb_plats["Baton de Frene"], 2)
        # 10 achetes >= 5 x 2 requis : fige tout de suite au prix courant.
        self.assertAlmostEqual(session.prix_fige["Baton de Frene"]["Bois de Frene"], 100.0)
        self.assertIn("Resine", session.ingredients_actifs)
        resultat = session.traiter_achat(Achat("10:00", 1, "Resine", 50))
        self.assertEqual(resultat.ingredient, "Resine")

    def test_ajout_d_une_recette_deja_presente_ignore(self):
        session = _session()
        doublon = Recette(1, "Blanquette de veau", [Ingredient(303, "Bois de Frene", 99)])
        self.assertEqual(session.ajouter_recettes([doublon]), [])
        self.assertEqual(len(session.recettes), 2)
        self.assertEqual(session.quantite_requise("Blanquette de veau", "Bois de Frene"), 3)

    def test_retrait_garde_les_cumuls_mais_plus_les_ingredients_orphelins(self):
        session = _session()
        session.correction_manuelle("Poivre Sichuan", 2, 200)
        self.assertTrue(session.retirer_recette("Blanquette de veau"))
        self.assertEqual([r.nom for r in session.etat()], ["Tarte aux pommes"])
        self.assertNotIn("Poivre Sichuan", session.ingredients_actifs)
        self.assertEqual(session.quantite_cumulee["Poivre Sichuan"], 2)
        self.assertEqual([l["nom"] for l in session.liste_courses()], ["Bois de Frene"])
        self.assertFalse(session.retirer_recette("Inconnue"))


class TestCorrectionDePrix(unittest.TestCase):
    """Rattrapage d'une saisie manuelle erronee."""

    def test_annuler_une_saisie_manuelle(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 2, 400)       # correcte
        mauvaise = session.correction_manuelle("Bois de Frene", 5, 999999)  # erreur
        self.assertGreater(session.prix_moyen("Bois de Frene"), 1000)

        self.assertTrue(session.annuler_correction(mauvaise))
        self.assertEqual(session.quantite_cumulee["Bois de Frene"], 2)
        self.assertEqual(session.euros_cumules["Bois de Frene"], 400)
        self.assertAlmostEqual(session.prix_moyen("Bois de Frene"), 200.0)

    def test_annulation_defige_si_le_cumul_ne_suffit_plus(self):
        session = _session()
        identifiant = session.correction_manuelle("Bois de Frene", 3, 300)
        self.assertIsNotNone(session.prix_fige["Blanquette de veau"].get("Bois de Frene"))
        session.annuler_correction(identifiant)
        self.assertIsNone(session.prix_fige["Blanquette de veau"].get("Bois de Frene"))

    def test_ajuster_le_cumul_a_la_main(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 10, 99999)  # prix moyen faux
        self.assertTrue(session.ajuster_cumul("Bois de Frene", 10, 1500))
        self.assertAlmostEqual(session.prix_moyen("Bois de Frene"), 150.0)
        self.assertAlmostEqual(
            session.prix_fige["Blanquette de veau"]["Bois de Frene"], 150.0)

    def test_prix_fige_impose_resiste_aux_achats_suivants(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 3, 300)
        self.assertTrue(session.definir_prix_fige("Blanquette de veau",
                                                  "Bois de Frene", 250.0))
        session.correction_manuelle("Bois de Frene", 100, 100000)
        self.assertAlmostEqual(
            session.prix_fige["Blanquette de veau"]["Bois de Frene"], 250.0)
        etat = {e.nom: e for e in session.etat()}["Blanquette de veau"]
        self.assertTrue(etat.ingredients[0].fige_manuellement)

    def test_defiger_rend_la_main_a_l_automatique(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 3, 300)
        session.definir_prix_fige("Blanquette de veau", "Bois de Frene", 250.0)
        session.defiger("Blanquette de veau", "Bois de Frene")
        # Cumul deja suffisant : refige aussitot, au vrai prix moyen.
        self.assertAlmostEqual(
            session.prix_fige["Blanquette de veau"]["Bois de Frene"], 100.0)


class TestRattrapage(unittest.TestCase):
    """Achats relus apres une perte d'alignement : surplus rattrape automatiquement
    si la confiance est haute (comme une lecture directe), propose sinon."""

    def setUp(self):
        self.session = _session()
        # Nom exact : haute confiance (>= seuil_auto), rattrape automatiquement.
        self.bois = Achat("01:40", 1, "Bois de Frene", 754)
        # Nom legerement mal lu par l'OCR : confiance moyenne (70-90), a confirmer.
        self.bois_incertain = Achat("01:40", 1, "Bois d Frn", 754)

    def test_accents_ignores_au_matching(self):
        session = Session([Recette(1, "Pot-au-feu", [
            Ingredient(1, "Jarret de Wagyū", 1)])], seuil_auto=90, seuil_confirmation=70)
        resultat = session.traiter_achat(Achat("02:30", 1, "Jarret de Wagyu", 2091))
        self.assertEqual(resultat.statut, "compte")

    def test_achat_haute_confiance_rattrape_automatiquement(self):
        self.assertEqual(self.session.proposer_rattrapages([self.bois]), (1, 0))
        self.assertEqual(self.session.achats_en_attente(), [])
        self.assertEqual(self.session.quantite_cumulee["Bois de Frene"], 1)

    def test_rattrape_auto_liste_puis_annulable(self):
        self.session.proposer_rattrapages([self.bois])
        liste = self.session.rattrapes()
        self.assertEqual(len(liste), 1)
        self.assertEqual(liste[0]["quantite"], 1)
        self.assertTrue(self.session.annuler_rattrape(liste[0]["identifiant"]))
        self.assertEqual(self.session.quantite_cumulee["Bois de Frene"], 0)
        self.assertEqual(self.session.rattrapes(), [])
        self.assertFalse(self.session.annuler_rattrape(liste[0]["identifiant"]))
        # Annule : ne doit pas etre repropose par la zone de securite.
        self.assertEqual(self.session.proposer_rattrapages([self.bois]), (0, 0))

    def test_achat_confiance_moyenne_propose_a_confirmer(self):
        self.assertEqual(self.session.proposer_rattrapages([self.bois_incertain]), (0, 1))
        attente = self.session.achats_en_attente()[0]
        self.assertTrue(attente.rattrape)
        self.assertEqual(self.session.quantite_cumulee["Bois de Frene"], 0)

    def test_achat_deja_compte_non_repropose(self):
        self.session.traiter_achat(self.bois)  # haute confiance -> compte directement
        self.assertEqual(self.session.proposer_rattrapages([self.bois]), (0, 0))
        # Affiche deux fois alors qu'un seul est compte : le second est rattrape.
        self.assertEqual(self.session.proposer_rattrapages([self.bois, self.bois]), (1, 0))

    def test_captures_repetees_ne_reproposent_pas(self):
        for _ in range(5):
            self.session.proposer_rattrapages([self.bois_incertain])
        self.assertEqual(len(self.session.achats_en_attente()), 1)

    def test_refus_definitif_et_pas_dans_les_non_identifies(self):
        self.session.proposer_rattrapages([self.bois_incertain])
        self.session.rejeter(self.session.achats_en_attente()[0].identifiant)
        self.assertEqual(self.session.proposer_rattrapages([self.bois_incertain]), (0, 0))
        self.assertEqual(self.session.non_identifies, [])


class TestPrixUnitaire(unittest.TestCase):
    """Prix unitaire saisi dans la liste d'achats."""

    def test_impose_pour_toutes_les_recettes_puis_rendu(self):
        session = _session()  # Bois de Frene dans l'epee et dans l'arc
        self.assertEqual(session.definir_prix_unitaire("Bois de Frene", 120), 2)
        self.assertEqual(session.prix_fige["Blanquette de veau"]["Bois de Frene"], 120)
        self.assertEqual(session.prix_fige["Tarte aux pommes"]["Bois de Frene"], 120)
        ligne = {l["nom"]: l for l in session.liste_courses()}["Bois de Frene"]
        self.assertEqual((ligne["prix_impose"], ligne["prix_total"]), (120, 13 * 120))

        session.traiter_achat(Achat("10:00", 13, "Bois de Frene", 13 * 50))
        self.assertEqual(session.prix_fige["Tarte aux pommes"]["Bois de Frene"], 120)
        session.definir_prix_unitaire("Bois de Frene", None)
        self.assertEqual(session.prix_fige["Tarte aux pommes"]["Bois de Frene"], 50)
        ligne = {l["nom"]: l for l in session.liste_courses()}["Bois de Frene"]
        self.assertIsNone(ligne["prix_impose"])


    def test_tout_en_stock_sans_achat(self):
        """Tout est deja en banque : le prix unitaire saisi suffit a chiffrer la recette."""
        session = Session([Recette(1, "Epee", [Ingredient(1, "Bois", 3), Ingredient(2, "Plume", 2)])])
        session.definir_stock("Bois", 3)
        session.definir_stock("Plume", 2)
        session.definir_prix_unitaire("Bois", 100)
        session.definir_prix_unitaire("Plume", 50)
        ligne = session.recapitulatif()[0]
        self.assertTrue(ligne["complet"])
        self.assertEqual(ligne["prix_achat"], 400)


class TestCuisiner(unittest.TestCase):
    """Bouton « Cuisine » : la recette consomme ses ingredients et repart a zero."""

    def test_consomme_les_achats_et_le_surplus_rejoint_le_stock(self):
        session = _session()  # Epee : 3 Bois + 2 Plumes ; Arc : 10 Bois
        session.definir_nb_plats("Blanquette de veau", 2)  # 6 Bois, 4 Plumes
        session.traiter_achat(Achat("10:00", 8, "Bois de Frene", 800))
        session.traiter_achat(Achat("10:01", 1, "Poivre Sichuan", 50))
        session.definir_stock("Poivre Sichuan", 5)
        session.definir_prix_fige("Blanquette de veau", "Poivre Sichuan", 70)

        self.assertTrue(session.cuisiner("Blanquette de veau"))
        # "achete" repart entierement a zero pour le prochain lot (pas
        # seulement la part utilisee pour celui-ci) ; le surplus (8 achetes -
        # 6 requis) rejoint le stock reel au lieu de rester compte comme
        # achat en cours.
        self.assertEqual(session.quantite_cumulee["Bois de Frene"], 0)
        self.assertEqual(session.euros_cumules["Bois de Frene"], 0)
        self.assertEqual(session.stock["Bois de Frene"], 2)
        # Le prix moyen observe (100/u) reste affiche comme reference.
        self.assertAlmostEqual(session.prix_moyen("Bois de Frene"), 100.0)
        self.assertEqual(session.quantite_cumulee["Poivre Sichuan"], 0)
        self.assertEqual(session.stock["Poivre Sichuan"], 2)  # 4 requis - 1 achete
        self.assertEqual(session.prix_fige["Blanquette de veau"], {})
        self.assertEqual(session.nb_plats["Blanquette de veau"], 2)

    def test_recette_inconnue(self):
        self.assertFalse(_session().cuisiner("Inconnue"))


class TestListeCourses(unittest.TestCase):
    """Besoins consolides sur toutes les recettes actives."""

    def test_une_ressource_partagee_est_additionnee(self):
        session = _session()  # Bois de Frene : 3 pour l'epee, 10 pour l'arc
        entrees = {e["nom"]: e for e in session.liste_courses()}
        self.assertEqual(entrees["Bois de Frene"]["besoin"], 13)
        self.assertEqual(
            sorted(entrees["Bois de Frene"]["recettes"]),
            ["Blanquette de veau", "Tarte aux pommes"])

    def test_le_nombre_de_plats_est_pris_en_compte(self):
        session = _session()
        session.definir_nb_plats("Tarte aux pommes", 20)  # 10 -> 200
        entrees = {e["nom"]: e for e in session.liste_courses()}
        self.assertEqual(entrees["Bois de Frene"]["besoin"], 3 + 200)

    def test_reste_a_acheter(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 5, 500)
        entrees = {e["nom"]: e for e in session.liste_courses()}
        self.assertEqual(entrees["Bois de Frene"]["achete"], 5)
        self.assertEqual(entrees["Bois de Frene"]["reste"], 8)
        self.assertAlmostEqual(entrees["Bois de Frene"]["prix_moyen"], 100.0)

    def test_surplus_ne_donne_pas_un_reste_negatif(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 100, 10000)
        entrees = {e["nom"]: e for e in session.liste_courses()}
        self.assertEqual(entrees["Bois de Frene"]["reste"], 0)

    def test_ce_qui_reste_a_acheter_est_en_tete(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 100, 10000)  # termine
        noms = [e["nom"] for e in session.liste_courses()]
        self.assertEqual(noms[-1], "Bois de Frene")  # termine : relegue en bas


class TestRecapitulatif(unittest.TestCase):
    """Un item par ligne : nom, quantite, prix total. Sans les ingredients."""

    def test_prix_total_quand_tout_est_fige(self):
        session = _session()
        session.definir_nb_plats("Blanquette de veau", 20)
        session.correction_manuelle("Bois de Frene", 60, 6000)       # 100 k/u
        session.correction_manuelle("Poivre Sichuan", 40, 20000)   # 500 k/u
        lignes = {e["nom"]: e for e in session.recapitulatif()}
        epee = lignes["Blanquette de veau"]
        self.assertTrue(epee["complet"])
        self.assertEqual(epee["quantite"], 20)
        self.assertAlmostEqual(epee["cout_total"], 26000.0)
        self.assertAlmostEqual(epee["cout_unitaire"], 1300.0)

    def test_estimation_tant_qu_un_ingredient_n_est_pas_fige(self):
        session = _session()
        session.definir_nb_plats("Blanquette de veau", 10)  # 30 bois, 20 plumes
        session.correction_manuelle("Bois de Frene", 30, 3000)   # fige a 100
        session.correction_manuelle("Poivre Sichuan", 5, 2500)  # 500 k/u, pas fige
        epee = {e["nom"]: e for e in session.recapitulatif()}["Blanquette de veau"]
        self.assertFalse(epee["complet"])
        self.assertIsNone(epee["cout_total"])
        # 30 x 100 (fige) + 20 x 500 (moyenne courante) = 13000
        self.assertAlmostEqual(epee["cout_estime"], 13000.0)
        self.assertEqual(epee["ingredients_manquants"], 1)

    def test_pas_d_estimation_sans_aucun_achat(self):
        session = _session()
        epee = {e["nom"]: e for e in session.recapitulatif()}["Blanquette de veau"]
        self.assertIsNone(epee["cout_estime"])
        self.assertIsNone(epee["cout_total"])

    def test_une_ligne_par_item_sans_detail_des_ingredients(self):
        session = _session()
        lignes = session.recapitulatif()
        self.assertEqual(len(lignes), 2)
        for ligne in lignes:
            self.assertNotIn("ingredients", ligne)
            self.assertEqual(
                set(ligne) & {"nom", "quantite", "cout_total"},
                {"nom", "quantite", "cout_total"})


class TestBenefice(unittest.TestCase):
    """Rentabilite : la commission de vente se preleve sur le PRIX DE VENTE."""

    def _session_prete(self):
        session = _session()
        session.correction_manuelle("Bois de Frene", 3, 3000)       # 1000 k/u
        session.correction_manuelle("Poivre Sichuan", 2, 2000)    # 1000 k/u
        return session  # cout de l'epee : 3x1000 + 2x1000 = 5000

    def test_benefice_avec_taxe_sur_le_prix_de_vente(self):
        session = self._session_prete()
        session.definir_prix_vente("Blanquette de veau", 10000)
        epee = {e["nom"]: e for e in session.recapitulatif()}["Blanquette de veau"]
        self.assertAlmostEqual(epee["prix_achat"], 5000.0)
        self.assertAlmostEqual(epee["taxe"], 200.0)        # 2 % de 10000
        self.assertAlmostEqual(epee["benefice"], 4800.0)   # 10000 - 200 - 5000
        self.assertAlmostEqual(epee["benefice_pourcent"], 96.0)

    def test_taxe_calculee_sur_la_vente_et_non_sur_la_marge(self):
        """Les deux lectures de '-2%' ne donnent pas le meme resultat."""
        session = self._session_prete()
        session.definir_prix_vente("Blanquette de veau", 10000)
        epee = {e["nom"]: e for e in session.recapitulatif()}["Blanquette de veau"]
        self.assertAlmostEqual(epee["benefice"], 4800.0)
        self.assertNotAlmostEqual(epee["benefice"], (10000 - 5000) * 0.98)  # 4900

    def test_prix_de_vente_et_d_achat_pour_la_quantite_totale(self):
        """On saisit le prix de vente du LOT : l'achat compare est celui du lot."""
        session = _session()
        session.definir_nb_plats("Blanquette de veau", 20)
        session.correction_manuelle("Bois de Frene", 60, 60000)     # 1000 k/u
        session.correction_manuelle("Poivre Sichuan", 40, 40000)  # 1000 k/u
        session.definir_prix_vente("Blanquette de veau", 200000)     # les 20 epees
        epee = {e["nom"]: e for e in session.recapitulatif()}["Blanquette de veau"]
        self.assertAlmostEqual(epee["prix_achat"], 100000.0)        # 20 x 5000
        self.assertAlmostEqual(epee["cout_unitaire"], 5000.0)
        self.assertAlmostEqual(epee["taxe"], 4000.0)                # 2 % de 200000
        self.assertAlmostEqual(epee["benefice"], 96000.0)           # 200000 - 4000 - 100000
        self.assertAlmostEqual(epee["benefice_total"], 96000.0)
        self.assertAlmostEqual(epee["benefice_pourcent"], 96.0)

    def test_benefice_negatif_quand_la_vente_ne_couvre_pas(self):
        session = self._session_prete()
        session.definir_prix_vente("Blanquette de veau", 4000)
        epee = {e["nom"]: e for e in session.recapitulatif()}["Blanquette de veau"]
        self.assertAlmostEqual(epee["benefice"], -1080.0)  # 4000 - 80 - 5000
        self.assertLess(epee["benefice_pourcent"], 0)

    def test_sans_prix_de_vente_pas_de_benefice(self):
        epee = {e["nom"]: e for e in self._session_prete().recapitulatif()}
        self.assertIsNone(epee["Blanquette de veau"]["prix_vente"])
        self.assertIsNone(epee["Blanquette de veau"]["benefice"])

    def test_effacer_le_prix_de_vente(self):
        session = self._session_prete()
        session.definir_prix_vente("Blanquette de veau", 10000)
        session.definir_prix_vente("Blanquette de veau", None)
        epee = {e["nom"]: e for e in session.recapitulatif()}["Blanquette de veau"]
        self.assertIsNone(epee["benefice"])

    def test_benefice_sur_cout_estime(self):
        """Un cout encore estime donne quand meme un benefice indicatif."""
        session = _session()
        session.definir_nb_plats("Blanquette de veau", 10)  # 30 bois, 20 plumes
        session.correction_manuelle("Bois de Frene", 30, 30000)   # fige, 1000 k/u
        session.correction_manuelle("Poivre Sichuan", 2, 2000)  # 1000 k/u, non fige
        session.definir_prix_vente("Blanquette de veau", 100000)  # les 10 epees
        epee = {e["nom"]: e for e in session.recapitulatif()}["Blanquette de veau"]
        self.assertFalse(epee["complet"])
        self.assertAlmostEqual(epee["prix_achat"], 50000.0)  # 30000 + 20 x 1000 estime
        self.assertAlmostEqual(epee["benefice"], 48000.0)    # 100000 - 2000 - 50000

    def test_taux_de_taxe_configurable(self):
        from catalogue import Ingredient, Recette

        recette = Recette(1, "R", [Ingredient(1, "X", 1)])
        session = Session([recette], 90, 70, taxe_pourcent=5.0)
        session.correction_manuelle("X", 1, 1000)
        session.definir_prix_vente("R", 2000)
        ligne = session.recapitulatif()[0]
        self.assertAlmostEqual(ligne["taxe"], 100.0)       # 5 % de 2000
        self.assertAlmostEqual(ligne["benefice"], 900.0)


class TestAmorcage(unittest.TestCase):
    """Pause / reprise : le chat deja affiche ne doit pas etre recompte."""

    def test_amorcage_ignore_la_premiere_capture(self):
        dedupeur = DedupeurDeFlux()
        dedupeur.amorcer()
        anciens = ["[04:30] 1 x [A] (10 euros)", "[04:31] 1 x [B] (20 euros)"]
        self.assertEqual(dedupeur.nouveaux(anciens), [])

        suite = anciens + ["[04:32] 1 x [C] (30 euros)"]
        self.assertEqual(dedupeur.nouveaux(suite),
                         ["[04:32] 1 x [C] (30 euros)"])

    def test_reprise_apres_pause_ne_rattrape_pas_le_retard(self):
        dedupeur = DedupeurDeFlux()
        dedupeur.nouveaux(["[04:30] 1 x [A] (10 euros)"])
        dedupeur.amorcer()  # pause, puis reprise : le chat a defile entre-temps
        pendant_pause = ["[04:3%d] 1 x [Z] (99 euros)" % i for i in range(1, 6)]
        self.assertEqual(dedupeur.nouveaux(pendant_pause), [])
        apres = pendant_pause + ["[04:37] 1 x [D] (40 euros)"]
        self.assertEqual(dedupeur.nouveaux(apres), ["[04:37] 1 x [D] (40 euros)"])


class TestMatching(unittest.TestCase):
    def test_seuils(self):
        session = _session()
        from parsing import Achat

        exact = session.traiter_achat(Achat("14:30", 1, "Bois de Frene", 100))
        self.assertEqual(exact.statut, STATUT_COMPTE)

        approx = session.traiter_achat(Achat("14:31", 1, "Bois de Frne", 100))
        self.assertIn(approx.statut, (STATUT_COMPTE, STATUT_A_CONFIRMER))

        inconnu = session.traiter_achat(Achat("14:32", 1, "Sauce tomate", 100))
        self.assertEqual(inconnu.statut, STATUT_REJETE)
        self.assertEqual(len(session.non_identifies), 1)

    def test_confirmation_manuelle_compte_l_achat(self):
        session = _session()
        from parsing import Achat

        resultat = session.traiter_achat(Achat("14:31", 5, "B0is de Frne", 500))
        if resultat.statut == STATUT_A_CONFIRMER:
            attente = session.achats_en_attente()[0]
            self.assertTrue(session.confirmer(attente.identifiant))
            self.assertEqual(session.quantite_cumulee["Bois de Frene"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
