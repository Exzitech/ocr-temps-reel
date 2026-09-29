"""Etat de session : matching des achats, cumuls, figeage des prix.

Logique de prix (le coeur du calcul) :
  - par ressource R : qty_cumulee[R] += quantite ; euros_cumules[R] += prix
  - prix_moyen[R] = euros_cumules[R] / qty_cumulee[R] a tout instant
  - stock[R] : unites deja possedees (chambre froide, reserve). Elles comptent pour
    atteindre le requis mais PAS dans la moyenne, qui reste celle des achats :
    le cout d'un ingredient vaut requis x prix moyen d'achat.
  - pour chaque recette active et chaque ingredient R qu'elle utilise :
    si (recette, R) n'est pas encore fige ET qty_cumulee[R] + stock[R] >= quantite
    requise par CETTE recette ET au moins une unite a ete achetee (sinon aucun
    prix), alors prix_fige[recette][R] = prix_moyen[R] a cet instant.

Le figeage est independant par recette : deux recettes utilisant la meme
ressource avec des quantites requises differentes se figent a des moments
differents, donc potentiellement a des prix moyens differents. C'est voulu.
"""
from __future__ import annotations

import logging
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz, process

from catalogue import Recette
from parsing import Achat

LOG = logging.getLogger(__name__)

STATUT_COMPTE = "compte"
STATUT_A_CONFIRMER = "a_confirmer"
STATUT_REJETE = "rejete"


def normaliser_nom(nom: str) -> str:
    """Casse et accents neutralises : 'Jarret de Wagyū' -> 'jarret de wagyu'.

    Les noms catalogue de recettes contiennent des lettres (ū...) que l'OCR ne sait pas lire :
    sans cela, leur score tombait sous le seuil.
    """
    decompose = unicodedata.normalize("NFKD", nom)
    return "".join(c for c in decompose if not unicodedata.combining(c)).lower().strip()


@dataclass
class ResultatAchat:
    achat: Achat
    statut: str
    ingredient: Optional[str] = None
    score: float = 0.0


@dataclass
class EtatIngredient:
    nom: str
    quantite_requise: int          # pour le nombre de plats demande
    quantite_cumulee: int
    prix_moyen: float
    prix_fige: Optional[float]
    quantite_unitaire: int = 0     # pour un seul plat
    euros_cumules: int = 0
    fige_manuellement: bool = False
    quantite_stock: int = 0        # deja possede (chambre froide, reserve)

    @property
    def est_fige(self) -> bool:
        return self.prix_fige is not None


@dataclass
class EtatRecette:
    nom: str
    ingredients: List[EtatIngredient] = field(default_factory=list)
    cout_total: Optional[float] = None
    nb_plats: int = 1

    @property
    def cout_unitaire(self) -> Optional[float]:
        if self.cout_total is None or self.nb_plats <= 0:
            return None
        return self.cout_total / self.nb_plats


@dataclass
class CorrectionManuelle:
    """Trace d'une saisie manuelle, pour pouvoir l'annuler."""
    identifiant: int
    ingredient: str
    quantite: int
    euros: int
    libelle: str


@dataclass
class RattrapeAuto:
    """Achat compte automatiquement par la zone de securite (voir
    proposer_rattrapages) : garde une trace pour pouvoir l'annuler."""
    identifiant: int
    heure: str
    ingredient: str
    quantite: int
    euros: int


@dataclass
class AchatEnAttente:
    identifiant: int
    achat: Achat
    ingredient_propose: str
    score: float
    # Relu dans une capture non alignee : peut-etre deja compte.
    rattrape: bool = False


class Session:
    """Session de plat : recettes actives + compteurs. Thread-safe."""

    def __init__(self, recettes: List[Recette], seuil_auto: int = 90,
                 seuil_confirmation: int = 70, taxe_pourcent: float = 2.0) -> None:
        self.seuil_auto = seuil_auto
        self.seuil_confirmation = seuil_confirmation
        # Commission de vente, prelevee sur le prix de vente.
        self.taux_taxe = taxe_pourcent / 100.0
        self._verrou = threading.RLock()
        self.demarree_le = time.time()
        self.recettes: List[Recette] = list(recettes)

        # Union des noms d'ingredients de TOUTES les recettes actives.
        self.ingredients_actifs: List[str] = sorted(
            {ing.nom for recette in self.recettes for ing in recette.ingredients}
        )

        self.quantite_cumulee: Dict[str, int] = {n: 0 for n in self.ingredients_actifs}
        self.euros_cumules: Dict[str, int] = {n: 0 for n in self.ingredients_actifs}
        # Unites deja possedees : comptent pour le requis, pas pour la moyenne.
        self.stock: Dict[str, int] = {n: 0 for n in self.ingredients_actifs}
        # Dernier prix moyen connu d'un ingredient dont le cumul d'achat vient
        # d'etre remis a zero (par cuisiner) : reference affichee tant qu'aucun
        # nouvel achat n'a ete relu.
        self.prix_moyen_memorise: Dict[str, float] = {}
        # prix_fige[nom_recette][nom_ingredient]
        self.prix_fige: Dict[str, Dict[str, float]] = {r.nom: {} for r in self.recettes}

        self.en_attente: Dict[int, AchatEnAttente] = {}
        self.non_identifies: List[Tuple[Achat, float]] = []
        # Noms OCR rattaches a la main a un ingredient, pour la suite de la session.
        self.alias: Dict[str, str] = {}
        # Nombre de plats vises par recette : multiplie les quantites requises.
        self.nb_plats: Dict[str, int] = {r.nom: 1 for r in self.recettes}
        # Prix figes a la main : ne sont jamais ecrases par le figeage auto.
        self.figes_manuels: Dict[str, Dict[str, bool]] = {r.nom: {} for r in self.recettes}
        self.corrections: List[CorrectionManuelle] = []
        # Achats rattrapes automatiquement (zone de securite), les plus recents
        # en dernier : visibles et annulables (un comptage silencieux doit
        # pouvoir etre verifie).
        self.rattrapes_auto: List[RattrapeAuto] = []
        self._prochain_rattrape = 1
        # Prix de vente unitaires saisis a la main, par recette.
        self.prix_vente: Dict[str, float] = {}
        self.nb_achats_comptes = 0
        # (heure, quantite, prix, ingredient) des achats comptes, proposes ou
        # refuses : sert a ne proposer que les achats relus en surnombre.
        self._signatures: Counter = Counter()
        self._prochain_id = 1
        self._prochaine_correction = 1

    # --------------------------------------------------------------- lecture
    def prix_moyen(self, ingredient: str) -> float:
        with self._verrou:
            quantite = self.quantite_cumulee.get(ingredient, 0)
            if quantite <= 0:
                # Plus d'achat en cours (typiquement apres un cuisine) : le
                # dernier prix moyen observe reste affiche comme reference.
                return self.prix_moyen_memorise.get(ingredient, 0.0)
            return self.euros_cumules[ingredient] / quantite

    # ------------------------------------------------------ recettes actives
    def ajouter_recettes(self, recettes: List[Recette],
                         plats: Optional[Dict[str, int]] = None) -> List[str]:
        """Ajoute des recettes a la session SANS toucher aux compteurs.

        Les achats deja comptes valent pour les nouvelles recettes : une
        ressource deja achetee en quantite suffisante fige tout de suite pour
        elles, au prix moyen courant. Une recette deja presente est ignoree.
        Retourne les noms reellement ajoutes.
        """
        plats = plats or {}
        with self._verrou:
            ajoutees = []
            for recette in recettes:
                if recette.nom in self.nb_plats:
                    continue
                self.recettes.append(recette)
                self.prix_fige[recette.nom] = {}
                self.figes_manuels[recette.nom] = {}
                self.nb_plats[recette.nom] = max(1, int(plats.get(recette.nom, 1)))
                for ingredient in recette.ingredients:
                    self.quantite_cumulee.setdefault(ingredient.nom, 0)
                    self.euros_cumules.setdefault(ingredient.nom, 0)
                    self.stock.setdefault(ingredient.nom, 0)
                ajoutees.append(recette.nom)
            if ajoutees:
                self._recalculer_ingredients_actifs()
                self._reevaluer_figeages()
                LOG.info("Recettes ajoutees : %s", ", ".join(ajoutees))
            return ajoutees

    def retirer_recette(self, nom_recette: str) -> bool:
        """Retire une recette. Les cumuls par ressource sont conserves."""
        with self._verrou:
            recette = next((r for r in self.recettes if r.nom == nom_recette), None)
            if recette is None:
                return False
            self.recettes.remove(recette)
            for table in (self.prix_fige, self.figes_manuels, self.nb_plats, self.prix_vente):
                table.pop(nom_recette, None)
            self._recalculer_ingredients_actifs()
            LOG.info("Recette retiree : %s", nom_recette)
            return True

    def cuisiner(self, nom_recette: str) -> bool:
        """Le lot est cuisine : ses ingredients sont consommes, la recette repart a zero.

        Pour chaque ingredient : ce qu'il fallait pour CE lot est pris d'abord
        sur les achats, puis sur le stock deja possede. Le cumul d'achat en
        cours repart entierement a zero (pas seulement la part utilisee pour
        ce lot) : « achete » ne doit plus compter, pour le prochain lot, ce qui
        a deja servi. Un eventuel surplus achete en trop rejoint le stock reel
        (les unites ne disparaissent pas, elles cessent juste d'etre comptees
        comme « en cours d'achat »). Le dernier prix moyen observe reste
        accessible via prix_moyen_memorise tant qu'aucun nouvel achat n'est lu.
        Les prix figes de la recette (manuels compris) sont effaces ; le prix
        de vente et le nombre de plats restent.
        """
        with self._verrou:
            recette = next((r for r in self.recettes if r.nom == nom_recette), None)
            if recette is None:
                return False
            plats = self.nb_plats.get(nom_recette, 1)
            for ingredient in recette.ingredients:
                nom = ingredient.nom
                besoin = ingredient.quantite_requise * plats
                achete = self.quantite_cumulee.get(nom, 0)
                if achete:
                    self.prix_moyen_memorise[nom] = self.prix_moyen(nom)
                utilise = min(achete, besoin)
                manque = besoin - utilise
                surplus = achete - utilise
                self.stock[nom] = max(0, self.stock.get(nom, 0) - manque) + surplus
                self.quantite_cumulee[nom] = 0
                self.euros_cumules[nom] = 0
                # Une saisie anterieure ne peut plus etre annulee sans fausser le reste.
                self.corrections = [c for c in self.corrections if c.ingredient != nom]
                self.rattrapes_auto = [r for r in self.rattrapes_auto if r.ingredient != nom]
            self.prix_fige[nom_recette] = {}
            self.figes_manuels[nom_recette] = {}
            self._reevaluer_figeages()
            LOG.info("Recette cuisinee : %s x%d, ingredients consommes", nom_recette, plats)
            return True

    def _recalculer_ingredients_actifs(self) -> None:
        # Seuls les ingredients encore utilises sont reconnus dans le chat.
        self.ingredients_actifs = sorted(
            {ing.nom for recette in self.recettes for ing in recette.ingredients})

    def disponible(self, ingredient: str) -> int:
        """Achete + deja possede : ce qui compte pour atteindre le requis."""
        return self.quantite_cumulee.get(ingredient, 0) + self.stock.get(ingredient, 0)

    def definir_stock(self, ingredient: str, quantite: int) -> bool:
        """Quantite deja possedee (chambre froide, reserve) pour une ressource.

        Les figeages sont reevalues : plus de stock peut figer plus tot, moins
        de stock peut defiger. Les prix deja figes ne changent pas (le stock
        n'entre pas dans la moyenne).
        """
        with self._verrou:
            if ingredient not in self.stock or quantite < 0:
                return False
            self.stock[ingredient] = quantite
            LOG.info("Stock %s : %d", ingredient, quantite)
            self._reevaluer_figeages()
            return True

    def quantite_requise(self, nom_recette: str, ingredient: str) -> int:
        """Quantite necessaire pour le nombre de plats demande."""
        for recette in self.recettes:
            if recette.nom == nom_recette:
                return recette.quantite_requise(ingredient) * self.nb_plats.get(
                    nom_recette, 1)
        return 0

    def etat(self) -> List[EtatRecette]:
        """Instantane complet pour l'UI."""
        with self._verrou:
            etats = []
            for recette in self.recettes:
                figes = self.prix_fige[recette.nom]
                manuels = self.figes_manuels[recette.nom]
                plats = self.nb_plats.get(recette.nom, 1)
                ingredients = [
                    EtatIngredient(
                        nom=ing.nom,
                        quantite_requise=ing.quantite_requise * plats,
                        quantite_unitaire=ing.quantite_requise,
                        quantite_cumulee=self.quantite_cumulee.get(ing.nom, 0),
                        euros_cumules=self.euros_cumules.get(ing.nom, 0),
                        prix_moyen=self.prix_moyen(ing.nom),
                        prix_fige=figes.get(ing.nom),
                        fige_manuellement=bool(manuels.get(ing.nom)),
                        quantite_stock=self.stock.get(ing.nom, 0),
                    )
                    for ing in recette.ingredients
                ]
                cout = None
                if ingredients and all(e.est_fige for e in ingredients):
                    cout = sum(e.quantite_requise * e.prix_fige for e in ingredients)
                etats.append(EtatRecette(nom=recette.nom, ingredients=ingredients,
                                         cout_total=cout, nb_plats=plats))
            return etats

    def liste_courses(self) -> List[dict]:
        """Besoins consolides sur TOUTES les recettes actives.

        Une meme ressource utilisee par plusieurs recettes est additionnee, en
        tenant compte du nombre de plats vise pour chacune. C'est la liste a
        avoir sous les yeux devant le logiciel du grossiste.
        """
        with self._verrou:
            besoins: Dict[str, dict] = {}
            for recette in self.recettes:
                plats = self.nb_plats.get(recette.nom, 1)
                for ingredient in recette.ingredients:
                    entree = besoins.setdefault(ingredient.nom, {
                        "nom": ingredient.nom, "besoin": 0, "achete": 0,
                        "stock": 0, "reste": 0, "recettes": [], "prix_moyen": 0.0,
                        "prix_total": None, "prix_impose": None,
                    })
                    entree["besoin"] += ingredient.quantite_requise * plats
                    if recette.nom not in entree["recettes"]:
                        entree["recettes"].append(recette.nom)
            for nom, entree in besoins.items():
                entree["achete"] = self.quantite_cumulee.get(nom, 0)
                entree["stock"] = self.stock.get(nom, 0)
                entree["reste"] = max(0, entree["besoin"] - self.disponible(nom))
                entree["prix_moyen"] = self.prix_moyen(nom)
                entree["prix_impose"] = self._prix_impose(nom)
                # Tout le besoin (stock compris) valorise au prix impose, sinon
                # au prix moyen d'achat.
                prix = entree["prix_impose"] or entree["prix_moyen"]
                if prix > 0:
                    entree["prix_total"] = entree["besoin"] * prix
            # Ce qu'il reste a acheter d'abord, puis par besoin decroissant.
            return sorted(besoins.values(),
                          key=lambda e: (e["reste"] == 0, -e["reste"], e["nom"]))

    def definir_prix_vente(self, nom_recette: str, prix: Optional[float]) -> None:
        """Prix de vente TOTAL (tous les plats) saisi a la main (None efface)."""
        with self._verrou:
            if prix is None or prix <= 0:
                self.prix_vente.pop(nom_recette, None)
            else:
                self.prix_vente[nom_recette] = float(prix)

    def recapitulatif(self) -> List[dict]:
        """Un item par ligne : cout d'achat, prix de vente, benefice. Sans detail.

        Tant qu'un ingredient n'est pas fige, le cout definitif n'existe pas :
        on fournit alors une estimation aux prix moyens du moment, clairement
        distinguee (`complet=False`), et None si une ressource n'a jamais ete
        achetee.

        Tout est exprime pour la QUANTITE TOTALE (nombre de plats vises) : on
        saisit ce que rapporte la vente du lot, on le compare a ce qu'a coute
        le lot. Le cout d'un seul plat reste fourni dans `cout_unitaire`.

        Benefice : la commission de vente se preleve sur le PRIX DE
        VENTE, pas sur la marge. D'ou
            benefice = prix_vente - taxe% x prix_vente - prix_achat
        """
        with self._verrou:
            lignes = []
            for etat in self.etat():
                manquants = [i for i in etat.ingredients if not i.est_fige]
                sans_prix = [i for i in manquants if i.prix_moyen <= 0]
                estime = None
                if not sans_prix:
                    estime = sum(
                        i.quantite_requise * (i.prix_fige if i.est_fige else i.prix_moyen)
                        for i in etat.ingredients)

                achat = etat.cout_total if etat.cout_total is not None else estime

                vente = self.prix_vente.get(etat.nom)
                taxe = benefice = pourcent = None
                if vente is not None:
                    taxe = vente * self.taux_taxe
                    if achat is not None:
                        benefice = vente - taxe - achat
                        if achat > 0:
                            pourcent = benefice / achat * 100.0

                lignes.append({
                    "nom": etat.nom,
                    "quantite": etat.nb_plats,
                    "cout_total": etat.cout_total,
                    "cout_estime": estime,
                    "cout_unitaire": etat.cout_unitaire,
                    "complet": etat.cout_total is not None,
                    "ingredients_manquants": len(manquants),
                    "prix_achat": achat,
                    "prix_vente": vente,
                    "taxe": taxe,
                    "benefice": benefice,
                    # Conserve pour l'UI tkinter : le benefice EST deja le total.
                    "benefice_total": benefice,
                    "benefice_pourcent": pourcent,
                })
            return lignes

    # ------------------------------------------------------------ nb de plats
    def definir_nb_plats(self, nom_recette: str, nombre: int) -> None:
        """Vise N exemplaires d'une recette : toutes ses quantites sont x N.

        Les seuils de figeage changent donc. On reevalue : un ingredient fige
        alors qu'il n'atteint plus le nouveau requis est defige (il se refigera
        quand le cumul y arrivera), et l'inverse fige immediatement. Un prix
        saisi a la main n'est jamais touche.
        """
        with self._verrou:
            if nom_recette not in self.nb_plats or nombre < 1:
                return
            if self.nb_plats[nom_recette] == nombre:
                return
            self.nb_plats[nom_recette] = nombre
            LOG.info("Nombre de plats %s : x%d", nom_recette, nombre)
            self._reevaluer_figeages()

    def achats_en_attente(self) -> List[AchatEnAttente]:
        with self._verrou:
            return list(self.en_attente.values())

    # -------------------------------------------------------------- matching
    def _matcher(self, nom_ocr: str) -> Tuple[Optional[str], float]:
        if not self.ingredients_actifs:
            return None, 0.0
        alias = self.alias.get(nom_ocr.strip().lower())
        if alias:
            return alias, 100.0
        resultat = process.extractOne(
            nom_ocr, self.ingredients_actifs, scorer=fuzz.token_sort_ratio,
            processor=normaliser_nom,
        )
        if resultat is None:
            return None, 0.0
        return resultat[0], float(resultat[1])

    def resume_non_identifies(self) -> List[dict]:
        """Noms lus mais rattaches a aucun ingredient, regroupes et tries.

        C'est ce qui manquait a l'ecran : des centaines d'achats pouvaient etre
        ecartes sans que rien ne le signale ailleurs que dans le journal.
        """
        with self._verrou:
            resume: Dict[str, dict] = {}
            for achat, score in self.non_identifies:
                entree = resume.setdefault(achat.nom_item, {
                    "nom": achat.nom_item, "occurrences": 0,
                    "quantite": 0, "euros": 0, "meilleur_score": 0.0,
                })
                entree["occurrences"] += 1
                entree["quantite"] += achat.quantite
                entree["euros"] += achat.prix_total
                entree["meilleur_score"] = max(entree["meilleur_score"], score)
            return sorted(resume.values(), key=lambda e: -e["occurrences"])

    def lier(self, nom_ocr: str, ingredient: str) -> int:
        """Rattache un nom lu a un ingredient et rattrape ses achats ecartes.

        Retourne le nombre d'achats recuperes. L'association vaut pour tout le
        reste de la session.
        """
        with self._verrou:
            if ingredient not in self.quantite_cumulee:
                return 0
            self.alias[nom_ocr.strip().lower()] = ingredient
            restants: List[Tuple[Achat, float]] = []
            recuperes = 0
            for achat, score in self.non_identifies:
                if achat.nom_item == nom_ocr:
                    self._appliquer(ingredient, achat.quantite, achat.prix_total)
                    self._signatures[_signature(achat, ingredient)] += 1
                    self.nb_achats_comptes += 1
                    recuperes += 1
                else:
                    restants.append((achat, score))
            self.non_identifies = restants
            LOG.info("Liaison %r -> %r : %d achat(s) recuperes",
                     nom_ocr, ingredient, recuperes)
            return recuperes

    def oublier_non_identifies(self, nom_ocr: str) -> None:
        with self._verrou:
            self.non_identifies = [
                (a, s) for a, s in self.non_identifies if a.nom_item != nom_ocr]

    def traiter_achat(self, achat: Achat) -> ResultatAchat:
        """Matche un achat contre les ingredients actifs et l'applique si sur."""
        with self._verrou:
            ingredient, score = self._matcher(achat.nom_item)
            if ingredient is None or score < self.seuil_confirmation:
                self.non_identifies.append((achat, score))
                LOG.info("Achat non identifie (score %.0f) : %r", score, achat.nom_item)
                return ResultatAchat(achat, STATUT_REJETE, ingredient, score)
            if score < self.seuil_auto:
                identifiant = self._prochain_id
                self._prochain_id += 1
                self.en_attente[identifiant] = AchatEnAttente(
                    identifiant, achat, ingredient, score
                )
                self._signatures[_signature(achat, ingredient)] += 1
                LOG.info("Achat a confirmer (score %.0f) : %r -> %r",
                         score, achat.nom_item, ingredient)
                return ResultatAchat(achat, STATUT_A_CONFIRMER, ingredient, score)

            self._appliquer(ingredient, achat.quantite, achat.prix_total)
            self._signatures[_signature(achat, ingredient)] += 1
            self.nb_achats_comptes += 1
            return ResultatAchat(achat, STATUT_COMPTE, ingredient, score)

    def proposer_rattrapages(self, achats: List[Achat]) -> Tuple[int, int]:
        """Achats relus dans une capture non alignee (perte d'alignement, ou
        zone de secours) : seul le SURPLUS par rapport a ce qui a deja ete
        compte, propose ou refuse est retenu (jamais recompte d'office).

        Meme seuil de confiance qu'une lecture directe (traiter_achat) : le
        surplus a haute confiance (score >= seuil_auto) est compte tout de
        suite, comme s'il avait ete lu normalement. Seul le surplus incertain
        est mis en attente de confirmation.

        Bug reel corrige : la zone de secours pouvait proposer des dizaines
        d'achats a confirmer d'un coup pendant une grosse rafale ; personne ne
        prend le temps de tous les verifier en pleine session d'achats, d'ou
        des erreurs de comptage au clic. Ne demander confirmation que pour les
        cas realment incertains reduit le nombre de clics et le risque d'erreur.

        Retourne (nombre rattrape automatiquement, nombre propose a confirmer).
        """
        with self._verrou:
            affiches: Counter = Counter()
            exemples: Dict[tuple, Tuple[Achat, str, float]] = {}
            for achat in achats:
                ingredient, score = self._matcher(achat.nom_item)
                if ingredient is None or score < self.seuil_confirmation:
                    continue
                cle = _signature(achat, ingredient)
                affiches[cle] += 1
                exemples[cle] = (achat, ingredient, score)
            comptes = 0
            a_confirmer = 0
            for cle, nombre in affiches.items():
                achat, ingredient, score = exemples[cle]
                for _ in range(nombre - self._signatures[cle]):
                    self._signatures[cle] += 1
                    if score >= self.seuil_auto:
                        self._appliquer(ingredient, achat.quantite, achat.prix_total)
                        self.nb_achats_comptes += 1
                        comptes += 1
                        self.rattrapes_auto.append(RattrapeAuto(
                            self._prochain_rattrape, achat.heure, ingredient,
                            achat.quantite, achat.prix_total))
                        self._prochain_rattrape += 1
                        del self.rattrapes_auto[:-200]
                    else:
                        identifiant = self._prochain_id
                        self._prochain_id += 1
                        self.en_attente[identifiant] = AchatEnAttente(
                            identifiant, achat, ingredient, score, rattrape=True)
                        a_confirmer += 1
            if comptes:
                LOG.info("%d achat(s) rattrape(s) automatiquement (haute confiance)", comptes)
            if a_confirmer:
                LOG.info("%d achat(s) relu(s) apres une perte d'alignement, a confirmer",
                         a_confirmer)
            return comptes, a_confirmer

    def rattrapes(self) -> List[dict]:
        """Achats rattrapes automatiquement, du plus recent au plus ancien."""
        with self._verrou:
            return [{"identifiant": r.identifiant, "heure": r.heure, "ingredient": r.ingredient,
                     "quantite": r.quantite, "euros": r.euros}
                    for r in reversed(self.rattrapes_auto)]

    def annuler_rattrape(self, identifiant: int) -> bool:
        """Defait un achat rattrape automatiquement (quantite ET euros).

        La signature reste marquee comme resolue : la zone de securite ne le
        repropose pas a la capture suivante.
        """
        with self._verrou:
            for indice, rattrape in enumerate(self.rattrapes_auto):
                if rattrape.identifiant != identifiant:
                    continue
                if self.quantite_cumulee.get(rattrape.ingredient, 0) < rattrape.quantite:
                    return False  # deja consomme par un plat
                self.quantite_cumulee[rattrape.ingredient] -= rattrape.quantite
                self.euros_cumules[rattrape.ingredient] = max(
                    0, self.euros_cumules[rattrape.ingredient] - rattrape.euros)
                self.nb_achats_comptes = max(0, self.nb_achats_comptes - 1)
                self.rattrapes_auto.pop(indice)
                LOG.info("Rattrapage #%d annule : -%d %s (-%d euros)", identifiant,
                         rattrape.quantite, rattrape.ingredient, rattrape.euros)
                self._reevaluer_figeages(recalculer_prix=True)
                return True
            return False

    def confirmer(self, identifiant: int, ingredient: Optional[str] = None) -> bool:
        """Valide un achat en attente (eventuellement vers un autre ingredient)."""
        with self._verrou:
            attente = self.en_attente.pop(identifiant, None)
            if attente is None:
                return False
            cible = ingredient or attente.ingredient_propose
            self._appliquer(cible, attente.achat.quantite, attente.achat.prix_total)
            self.nb_achats_comptes += 1
            LOG.info("Achat confirme manuellement : %r -> %r", attente.achat.nom_item, cible)
            return True

    def rejeter(self, identifiant: int) -> bool:
        with self._verrou:
            attente = self.en_attente.pop(identifiant, None)
            if attente is None:
                return False
            # Un rattrapage refuse etait un doublon, pas un nom inconnu.
            if not attente.rattrape:
                self.non_identifies.append((attente.achat, attente.score))
            return True

    # ------------------------------------------------------------- correction
    def correction_manuelle(self, ingredient: str, quantite: int,
                            prix_total: Optional[int] = None) -> int:
        """Incremente manuellement une ressource ratee par l'OCR.

        Sans prix, on applique le prix moyen courant : la quantite avance sans
        deformer la moyenne (et donc sans fausser un figeage a venir).
        Retourne l'identifiant de la correction, pour pouvoir l'annuler.
        """
        with self._verrou:
            if quantite <= 0:
                return 0
            if prix_total is None:
                prix_total = int(round(self.prix_moyen(ingredient) * quantite))
            self._appliquer(ingredient, quantite, prix_total)
            identifiant = self._prochaine_correction
            self._prochaine_correction += 1
            self.corrections.append(CorrectionManuelle(
                identifiant, ingredient, quantite, prix_total,
                "+%d (%d euros)" % (quantite, prix_total)))
            LOG.info("Correction manuelle #%d : %+d %s (%d euros)",
                     identifiant, quantite, ingredient, prix_total)
            return identifiant

    def corrections_de(self, ingredient: str) -> List[CorrectionManuelle]:
        with self._verrou:
            return [c for c in self.corrections if c.ingredient == ingredient]

    def annuler_correction(self, identifiant: int) -> bool:
        """Defait une saisie manuelle erronee (quantite ET euros)."""
        with self._verrou:
            for indice, correction in enumerate(self.corrections):
                if correction.identifiant != identifiant:
                    continue
                self.quantite_cumulee[correction.ingredient] -= correction.quantite
                self.euros_cumules[correction.ingredient] -= correction.euros
                self.corrections.pop(indice)
                LOG.info("Correction #%d annulee : -%d %s (-%d euros)",
                         identifiant, correction.quantite, correction.ingredient,
                         correction.euros)
                self._reevaluer_figeages(recalculer_prix=True)
                return True
            return False

    def ajuster_cumul(self, ingredient: str, quantite: int, euros: int) -> bool:
        """Remet a plat le cumul d'une ressource (quantite et euros exacts).

        Le recours quand une saisie manuelle a fausse le prix moyen : on donne
        directement les bonnes valeurs plutot que de bricoler des deltas.
        """
        with self._verrou:
            if ingredient not in self.quantite_cumulee or quantite < 0 or euros < 0:
                return False
            self.quantite_cumulee[ingredient] = quantite
            self.euros_cumules[ingredient] = euros
            LOG.info("Cumul corrige : %s = %d u / %d euros (moyenne %.2f)",
                     ingredient, quantite, euros, self.prix_moyen(ingredient))
            self._reevaluer_figeages(recalculer_prix=True)
            return True

    # ------------------------------------------------------------ prix figes
    def _prix_impose(self, ingredient: str) -> Optional[float]:
        """Prix fige a la main pour cet ingredient (premiere recette qui en a un)."""
        for recette in self.recettes:
            if self.figes_manuels.get(recette.nom, {}).get(ingredient):
                return self.prix_fige[recette.nom].get(ingredient)
        return None

    def definir_prix_unitaire(self, ingredient: str, prix: Optional[float]) -> int:
        """Prix unitaire saisi dans la liste d'achats, pour TOUTES les recettes.

        None rend la main au figeage automatique. Retourne le nombre de
        recettes concernees.
        """
        with self._verrou:
            recettes = [r.nom for r in self.recettes
                        if any(i.nom == ingredient for i in r.ingredients)]
            for nom in recettes:
                if prix is None:
                    self.defiger(nom, ingredient)
                else:
                    self.definir_prix_fige(nom, ingredient, prix)
            return len(recettes)

    def definir_prix_fige(self, nom_recette: str, ingredient: str,
                          prix: float) -> bool:
        """Impose un prix figé pour un couple (recette, ingredient).

        Marque comme manuel : le figeage automatique ne le reecrira jamais.
        """
        with self._verrou:
            if nom_recette not in self.prix_fige or prix < 0:
                return False
            self.prix_fige[nom_recette][ingredient] = float(prix)
            self.figes_manuels[nom_recette][ingredient] = True
            LOG.info("Prix fige a la main : %s / %s = %.2f k/u",
                     nom_recette, ingredient, prix)
            return True

    def defiger(self, nom_recette: str, ingredient: str) -> None:
        """Rend la main au figeage automatique pour ce couple."""
        with self._verrou:
            self.prix_fige.get(nom_recette, {}).pop(ingredient, None)
            self.figes_manuels.get(nom_recette, {}).pop(ingredient, None)
            LOG.info("Defige : %s / %s", nom_recette, ingredient)
            self._tenter_figeage(ingredient)

    def _reevaluer_figeages(self, recalculer_prix: bool = False) -> None:
        """Reaccorde les figeages automatiques aux cumuls et seuils courants.

        recalculer_prix : recalcule aussi les prix deja figes automatiquement.
        A n'utiliser que lorsque le cumul a ete corrige a la main - un prix fige
        a partir d'une moyenne fausse doit repartir de la moyenne rectifiee.
        Le flux normal des achats, lui, ne refige jamais (fige une seule fois).
        """
        for recette in self.recettes:
            figes = self.prix_fige[recette.nom]
            manuels = self.figes_manuels[recette.nom]
            plats = self.nb_plats.get(recette.nom, 1)
            for ingredient in recette.ingredients:
                if manuels.get(ingredient.nom):
                    continue  # saisi a la main : intouchable
                requis = ingredient.quantite_requise * plats
                cumul = self.disponible(ingredient.nom)
                if cumul < requis or self.quantite_cumulee.get(ingredient.nom, 0) <= 0:
                    figes.pop(ingredient.nom, None)
                elif ingredient.nom not in figes or recalculer_prix:
                    figes[ingredient.nom] = self.prix_moyen(ingredient.nom)
                    LOG.info("FIGE %s / %s : %.2f euros/unite (cumul %d >= requis %d)",
                             recette.nom, ingredient.nom,
                             figes[ingredient.nom], cumul, requis)

    # ----------------------------------------------------------------- calcul
    def _appliquer(self, ingredient: str, quantite: int, prix_total: int) -> None:
        """Cumule puis tente de figer. A appeler sous verrou."""
        if ingredient not in self.quantite_cumulee:
            LOG.warning("Ingredient hors session ignore : %r", ingredient)
            return
        self.quantite_cumulee[ingredient] += quantite
        self.euros_cumules[ingredient] += prix_total
        LOG.info(
            "%s : +%d (%d euros) -> cumul %d, moyenne %.2f k/u",
            ingredient, quantite, prix_total,
            self.quantite_cumulee[ingredient], self.prix_moyen(ingredient),
        )
        self._tenter_figeage(ingredient)

    def _tenter_figeage(self, ingredient: str) -> None:
        """Fige (recette, ingredient) des que le cumul atteint le requis de CETTE recette."""
        moyenne = self.prix_moyen(ingredient)
        cumul = self.disponible(ingredient)
        if self.quantite_cumulee[ingredient] <= 0:
            return  # rien d'achete : aucun prix a figer, meme si le stock suffit
        for recette in self.recettes:
            # Le seuil suit le nombre de plats vises (x20 items = x20 ingredients).
            requis = recette.quantite_requise(ingredient) * self.nb_plats.get(
                recette.nom, 1)
            if requis <= 0:
                continue
            figes = self.prix_fige[recette.nom]
            if ingredient in figes:
                continue
            if cumul >= requis:
                figes[ingredient] = moyenne
                LOG.info(
                    "FIGE %s / %s : %.2f euros/unite (cumul %d >= requis %d)",
                    recette.nom, ingredient, moyenne, cumul, requis,
                )

    # ------------------------------------------------------------------ divers
    def taille_queue_ingredients(self) -> int:
        return len(self.ingredients_actifs)


def _signature(achat: Achat, ingredient: str) -> tuple:
    """Cle de reconciliation entre les achats deja comptes/proposes et un
    rattrapage (voir proposer_rattrapages). Le prix est regroupe par tranche
    de 10 euros (division entiere, pas d'arrondi au plus proche : evite qu'un
    couple comme 4034/4035 tombe de part et d'autre d'une frontiere).

    Bug reel corrige : deux zones de capture qui lisent le meme achat via des
    decoupes en pixels differentes peuvent en lire le prix legerement
    differemment (bruit OCR sur un chiffre, ex. 4034 contre 4035 euros) ; sans
    ce regroupement, la MEME transaction reelle prenait deux signatures
    differentes et etait comptee deux fois. Dans le doute, mieux vaut fusionner
    deux achats trop proches en prix que gonfler silencieusement le total
    (meme principe que la deduplication positionnelle, voir DedupeurDeFlux).
    """
    return (achat.heure, achat.quantite, achat.prix_total // 10, ingredient)
