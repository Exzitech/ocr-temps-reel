"""Test d'integration du moteur : on lui injecte des textes OCR successifs
(comme le ferait le thread consommateur) sans capture d'ecran ni Tesseract.

Lancement (depuis tools/cout-plats) : python -m unittest tests.test_moteur -v
"""
from __future__ import annotations

import unittest

import config as module_config
from catalogue import Ingredient, Recette
from moteur import Moteur
from tracker import Session


def _moteur_sans_pipeline(recettes):
    configuration = module_config.CONFIG_DEFAUT
    moteur = Moteur(configuration)
    seuils = configuration["matching"]
    moteur.session = Session(recettes, seuils["seuil_auto"], seuils["seuil_confirmation"])
    return moteur


class TestMoteur(unittest.TestCase):
    def setUp(self):
        self.recette = Recette(1, "Salade de fruits", [
            Ingredient(1, "Gelee Myrtille", 20),
            Ingredient(2, "Gelee Citron", 2),
        ])
        self.moteur = _moteur_sans_pipeline([self.recette])

    def test_flux_de_captures_chevauchantes(self):
        """Le chat defile ; chaque achat n'est compte qu'une fois."""
        captures = [
            "[14:30] 10 x [Gelee Myrtille] (200 euros)\n",
            # meme capture relue plusieurs fois par le pipeline
            "[14:30] 10 x [Gelee Myrtille] (200 euros)\n",
            "[14:30] 10 x [Gelee Myrtille] (200 euros)\n"
            "[14:31] Banque : + 5000 Euros (vente de Salade de fruits)\n",
            # deux achats identiques d'affilee : deux transactions distinctes
            "[14:30] 10 x [Gelee Myrtille] (200 euros)\n"
            "[14:31] Banque : + 5000 Euros (vente de Salade de fruits)\n"
            "[14:32] 1 x [Gelee Citron] (500 euros)\n"
            "[14:32] 1 x [Gelee Citron] (500 euros)\n",
            # nom coupe sur deux lignes par le client
            "[14:32] 1 x [Gelee Citron] (500 euros)\n"
            "[14:32] 1 x [Gelee Citron] (500 euros)\n"
            "[14:33] 10 x [Gelee\nMyrtille] (600 euros)\n",
        ]
        for capture in captures:
            self.moteur._sur_texte(capture)

        session = self.moteur.session
        self.assertEqual(session.quantite_cumulee["Gelee Myrtille"], 20)   # 10 + 10
        self.assertEqual(session.euros_cumules["Gelee Myrtille"], 800)    # 200 + 600
        self.assertEqual(session.quantite_cumulee["Gelee Citron"], 2)   # 2 x 1
        self.assertEqual(session.euros_cumules["Gelee Citron"], 1000)
        self.assertEqual(session.nb_achats_comptes, 4)

        etat = session.etat()[0]
        self.assertAlmostEqual(etat.ingredients[0].prix_fige, 40.0)  # 800/20
        self.assertAlmostEqual(etat.ingredients[1].prix_fige, 500.0)  # 1000/2
        # 20 x 40 + 2 x 500 = 1800
        self.assertAlmostEqual(etat.cout_total, 1800.0)

    def test_session_reelle_langue_de_veau(self):
        """Rejoue la session qui perdait des ingredients (capture d'ecran).

        Seuls Crane (195 k) et Pierre (894 k) etaient comptes : les trois
        autres depassent 1000 euros, donc portaient un separateur de milliers
        que la regex refusait.
        """
        recette = Recette(2, "Langue de veau", [
            Ingredient(1, "Fragment d'Épée Reptilienne", 2),
            Ingredient(2, "Ris de veau", 9),
            Ingredient(3, "Tete de veau", 1),
            Ingredient(4, "Fleur de sel royale", 3),
            Ingredient(5, "Piment cramoisi", 2),
        ])
        moteur = _moteur_sans_pipeline([recette])
        moteur._sur_texte(
            "[04:29] Vous avez perdu 20 euros.\n"
            "[04:29] 1 x [Fragment d'Épée Reptilienne] (1 067 euros)\n"
            "[04:29] 1 x [Fragment d'Épée Reptilienne] (1 088 euros)\n"
            "[04:30] 1 x [Tete de veau] (195 euros)\n"
            "[04:31] 1 x [Fleur de sel royale] (894 euros)\n"
            "[04:31] 1 x [Piment cramoisi] (2 299 euros)\n"
            "[04:31] 1 x [Piment cramoisi] (2 299 euros)\n"
        )
        session = moteur.session
        self.assertEqual(session.nb_achats_comptes, 6)
        self.assertEqual(session.quantite_cumulee["Fragment d'Épée Reptilienne"], 2)
        self.assertEqual(session.quantite_cumulee["Piment cramoisi"], 2)
        self.assertEqual(session.euros_cumules["Fragment d'Épée Reptilienne"], 2155)
        self.assertEqual(len(session.non_identifies), 0)

        etat = session.etat()[0]
        figes = {i.nom: i.prix_fige for i in etat.ingredients}
        self.assertAlmostEqual(figes["Fragment d'Épée Reptilienne"], 1077.5)
        self.assertAlmostEqual(figes["Piment cramoisi"], 2299.0)

    def test_chat_immobile_ne_recompte_pas(self):
        """Le chat ne bouge plus : relire la meme fenetre ne doit rien ajouter."""
        recette = Recette(3, "Lame", [Ingredient(4, "Fleur de sel", 3)])
        moteur = _moteur_sans_pipeline([recette])
        fenetre = "".join(
            "[04:31] 1 x [Fleur de sel] (894 euros)\n" for _ in range(8))
        for _ in range(25):
            moteur._sur_texte(fenetre)
        self.assertEqual(moteur.session.quantite_cumulee["Fleur de sel"], 8)

    def test_rafale_40_achats_identiques(self):
        """40 ecailles a 1066 euros d'affilee, 3-4 achats/seconde.

        Cas reel decrit par l'utilisateur. Tant que la fenetre de chat visible
        est assez haute pour contenir la rafale, tout doit etre compte : les
        lignes sont rigoureusement identiques, seule leur POSITION les
        distingue.
        """
        nom = "Ris de veau"
        ligne = "[04:31] 1 x [%s] (1 066 euros)\n" % nom
        moteur = _moteur_sans_pipeline([Recette(9, "Lame", [Ingredient(1, nom, 9)])])

        # Fenetre de 45 lignes, ~2 achats entre deux captures (3.5/s, 0.55 s).
        visibles = []
        for numero in range(40):
            visibles.append(ligne)
            if numero % 2 == 1:  # une capture toutes les deux emissions
                moteur._sur_texte("".join(visibles[-45:]))
        moteur._sur_texte("".join(visibles[-45:]))

        session = moteur.session
        self.assertEqual(session.quantite_cumulee[nom], 40)
        self.assertEqual(session.euros_cumules[nom], 40 * 1066)
        self.assertAlmostEqual(session.prix_moyen(nom), 1066.0)
        self.assertEqual(moteur.dedupeur.resynchronisations, 0)

    def test_rafale_qui_deborde_la_fenetre_est_signalee(self):
        """Fenetre trop courte : on sous-compte, mais on le DIT.

        Quand tout le chat visible est occupe par le meme achat, deux captures
        successives sont identiques au pixel pres : l'information n'existe pas.
        Le seul comportement correct est de le signaler.
        """
        nom = "Ris de veau"
        ligne = "[04:31] 1 x [%s] (1 066 euros)\n" % nom
        moteur = _moteur_sans_pipeline([Recette(9, "Lame", [Ingredient(1, nom, 9)])])

        visibles = []
        for numero in range(40):
            visibles.append(ligne)
            if numero % 2 == 1:
                moteur._sur_texte("".join(visibles[-10:]))  # chat de 10 lignes

        session = moteur.session
        self.assertLess(session.quantite_cumulee[nom], 40)   # sous-compte assume
        self.assertGreater(moteur.dedupeur.captures_saturees, 0)
        alerte = moteur.alerte_saturation()
        self.assertIsNotNone(alerte)
        self.assertIn(nom, alerte)

    def test_texte_illisible_ne_casse_rien(self):
        self.moteur._sur_texte("~~~ bruit ###")
        self.moteur._sur_texte("")
        self.assertIsNone(self.moteur.derniere_erreur)
        self.assertEqual(self.moteur.session.nb_achats_comptes, 0)


class TestZoneSecurite(unittest.TestCase):
    """source="securite" (zone de secours, optionnelle) : ne doit jamais
    recompter ce que la zone rapide a deja vu, ni toucher a son suivi
    positionnel. Bug reel corrige : melanger les deux dans le meme suivi
    positionnel faisait ressurgir de vieux achats comme nouveaux (double
    comptage) quand l'utilisateur confirmait "a confirmer" en pensant que
    c'etaient de nouveaux achats de rafale.
    """

    def setUp(self):
        self.nom = "Eau Ferrugineuse"
        recette = Recette(1, "Slip", [Ingredient(1, self.nom, 20)])
        self.moteur = _moteur_sans_pipeline([recette])

    def test_ne_recompte_pas_ce_que_la_zone_rapide_a_deja_compte(self):
        ligne = "[11:02] 1 x [%s] (375 euros)\n" % self.nom
        self.moteur._sur_texte(ligne * 3)  # zone rapide : 3 achats comptes
        self.assertEqual(self.moteur.session.quantite_cumulee[self.nom], 3)

        # La zone de secours relit une fenetre large qui montre les 3 MEMES
        # achats (elle n'avance pas au meme rythme que la zone rapide).
        self.moteur._sur_texte(ligne * 3, source="securite")

        self.assertEqual(self.moteur.session.quantite_cumulee[self.nom], 3)
        self.assertEqual(len(self.moteur.session.en_attente), 0)

    def test_prix_legerement_different_entre_les_deux_zones_pas_double_compte(self):
        """Bug reel : la zone de secours lit un prix a 1 euros pres de la zone
        rapide pour le MEME achat (decoupe en pixels differente), et le
        comptait une deuxieme fois faute de tolerance sur le prix."""
        self.moteur._sur_texte("[11:34] 10 x [%s] (4034 euros)\n" % self.nom)
        self.assertEqual(self.moteur.session.quantite_cumulee[self.nom], 10)

        # Zone de secours : meme achat, prix lu 1 euros plus haut.
        self.moteur._sur_texte("[11:34] 10 x [%s] (4035 euros)\n" % self.nom, source="securite")

        self.assertEqual(self.moteur.session.quantite_cumulee[self.nom], 10)
        self.assertEqual(len(self.moteur.session.en_attente), 0)

    def test_propose_seulement_le_surplus_manque_par_la_zone_rapide(self):
        ligne = "[11:09] 1 x [%s] (375 euros)\n" % self.nom
        # Rafale trop rapide pour la petite fenetre : elle n'en voit que 2/5.
        self.moteur._sur_texte(ligne * 2)
        self.assertEqual(self.moteur.session.quantite_cumulee[self.nom], 2)

        # La zone de secours (fenetre plus large) voit les 5 vrais achats.
        self.moteur._sur_texte(ligne * 5, source="securite")

        # Seul le surplus reel (3) est rattrape - jamais les 2 deja comptes -
        # et automatiquement puisque le nom correspond avec une confiance
        # maximale (comme une lecture directe, pas de popup a cliquer).
        self.assertEqual(self.moteur.session.quantite_cumulee[self.nom], 5)
        self.assertEqual(len(self.moteur.session.en_attente), 0)

    def test_ne_touche_pas_au_suivi_positionnel_de_la_zone_rapide(self):
        ligne = "[11:02] 1 x [%s] (375 euros)\n" % self.nom
        self.moteur._sur_texte(ligne)
        historique_avant = self.moteur.dedupeur.historique
        self.moteur._sur_texte("texte totalement different, sans aucun rapport" * 5,
                               source="securite")
        self.assertEqual(self.moteur.dedupeur.historique, historique_avant)
        self.assertEqual(self.moteur.dedupeur.resynchronisations, 0)

    def test_confiance_moyenne_reste_a_confirmer_via_la_zone_de_secours(self):
        """Nom mal lu (confiance moyenne) : jamais compte d'office, meme en rattrapage."""
        ligne = "[11:09] 1 x [Eau X Ferrugineuz] (375 euros)\n"
        self.moteur._sur_texte(ligne, source="securite")
        self.assertEqual(self.moteur.session.quantite_cumulee[self.nom], 0)
        self.assertEqual(len(self.moteur.session.en_attente), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
