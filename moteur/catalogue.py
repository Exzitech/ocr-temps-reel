"""Client de l'API du catalogue (https://api.exemple.fr) : recherche d'item + recette.

Enchainement verifie contre l'API reelle :
  1. GET /items?slug.fr[$search]=<nom>   -> id de l'item
  2. GET /recipes?resultId=<id>          -> ingredientIds + quantities
  3. GET /items?id[$in][]=...            -> noms francais des ingredients
"""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

LOG = logging.getLogger(__name__)

BASE_URL = "https://api.exemple.fr"
TIMEOUT = 15
# L'API plafonne toute reponse a 50 elements, quel que soit $limit demande.
TAILLE_LOT = 50


class ErreurAPI(Exception):
    """API du catalogue injoignable ou reponse inexploitable."""


@dataclass
class Ingredient:
    item_id: int
    nom: str
    quantite_requise: int


@dataclass
class Recette:
    item_id: int
    nom: str
    ingredients: List[Ingredient] = field(default_factory=list)

    def quantite_requise(self, nom_ingredient: str) -> int:
        for ing in self.ingredients:
            if ing.nom == nom_ingredient:
                return ing.quantite_requise
        return 0


def normaliser(texte: str) -> str:
    """Minuscules sans accents : sert au classement des resultats de recherche."""
    decompose = unicodedata.normalize("NFD", texte or "")
    sans_accent = "".join(c for c in decompose if unicodedata.category(c) != "Mn")
    return sans_accent.lower().strip()


class ClientCatalogue:
    def __init__(self, base_url: str = BASE_URL, timeout: int = TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ExziHub-CoutPlats/1.0"})
        self._cache_recettes: Dict[str, Recette] = {}

    def _get(self, chemin: str, params: dict) -> dict:
        url = self.base_url + chemin
        try:
            reponse = self.session.get(url, params=params, timeout=self.timeout)
            reponse.raise_for_status()
            return reponse.json()
        except requests.RequestException as exc:
            raise ErreurAPI("API du catalogue injoignable (%s) : %s" % (url, exc)) from exc
        except ValueError as exc:
            raise ErreurAPI("Reponse non-JSON de %s : %s" % (url, exc)) from exc

    # ------------------------------------------------------------------ items
    def _recherche_brute(self, terme: str) -> List[dict]:
        return self._get(
            "/items",
            {
                "slug.fr[$search]": terme,
                "$limit": 20,
                "$select[]": ["id", "name", "level"],
            },
        ).get("data", [])

    def chercher_items(self, nom: str) -> List[dict]:
        """Candidats pour un nom francais, du plus probable au moins probable.

        Le champ slug.fr de l'API est DESACCENTUE : chercher "Blanquette de veau"
        trouve l'item, chercher "Epee" avec son accent ne trouve rien. On envoie
        donc systematiquement la forme normalisee.
        """
        cible = normaliser(nom)
        donnees = self._recherche_brute(cible)
        if not donnees and cible != nom.strip().lower():
            donnees = self._recherche_brute(nom)
        if not donnees:
            return []

        def score(item: dict) -> tuple:
            nom_fr = normaliser((item.get("name") or {}).get("fr", ""))
            return (nom_fr != cible, not nom_fr.startswith(cible), len(nom_fr))

        return sorted(donnees, key=score)

    def chercher_item(self, nom: str) -> Optional[dict]:
        """Meilleur item correspondant au nom francais donne."""
        candidats = self.chercher_items(nom)
        return candidats[0] if candidats else None

    def noms_items(self, item_ids: List[int]) -> Dict[int, str]:
        """Noms francais pour une liste d'ids, par lots de 50 (plafond de l'API)."""
        uniques = list(dict.fromkeys(item_ids))
        noms: Dict[int, str] = {}
        for debut in range(0, len(uniques), TAILLE_LOT):
            lot = uniques[debut:debut + TAILLE_LOT]
            params = {"$limit": len(lot), "$select[]": ["id", "name"], "id[$in][]": lot}
            for item in self._get("/items", params).get("data", []):
                noms[item["id"]] = (item.get("name") or {}).get("fr", "")
        return noms

    # ---------------------------------------------------------------- recettes
    def recette(self, nom_item: str) -> Recette:
        """Recette complete (nom + ingredients) pour un nom d'item francais."""
        cle = normaliser(nom_item)
        if cle in self._cache_recettes:
            return self._cache_recettes[cle]

        candidats = self.chercher_items(nom_item)
        if not candidats:
            raise ErreurAPI("Item introuvable dans le catalogue : %r" % nom_item)

        # Le meilleur candidat n'est pas forcement realisable (ressource
        # homonyme, version non realisable) : on essaie les suivants.
        brute = None
        nom_officiel = (candidats[0].get("name") or {}).get("fr", nom_item)
        item_id = candidats[0]["id"]
        for candidat in candidats[:8]:
            donnees = self._get(
                "/recipes", {"resultId": candidat["id"], "$limit": 1}).get("data", [])
            if donnees:
                brute = donnees[0]
                item_id = candidat["id"]
                nom_officiel = (candidat.get("name") or {}).get("fr", nom_item)
                break
        if brute is None:
            raise ErreurAPI("Aucune recette pour %r (item %s) : item non realisable ?"
                            % (nom_officiel, item_id))
        ids = brute.get("ingredientIds") or []
        quantites = brute.get("quantities") or []
        if len(ids) != len(quantites):
            raise ErreurAPI("Recette %r incoherente (ids/quantites)" % nom_officiel)

        noms = self.noms_items(ids)
        ingredients = []
        for ingredient_id, quantite in zip(ids, quantites):
            nom = noms.get(ingredient_id)
            if not nom:
                LOG.warning("Nom manquant pour l'ingredient %s de %r", ingredient_id, nom_officiel)
                nom = "Item %s" % ingredient_id
            ingredients.append(Ingredient(ingredient_id, nom, int(quantite)))

        recette = Recette(item_id=item_id, nom=nom_officiel, ingredients=ingredients)
        self._cache_recettes[cle] = recette
        LOG.info("Recette chargee : %s (%d ingredients)", nom_officiel, len(ingredients))
        return recette

    def recettes_par_ids(self, item_ids: List[int]) -> tuple:
        """Recettes d'items designes par leur id (catalogue du hub).

        Sans ambiguite de nom, et en quelques requetes quel que soit le nombre
        d'items. Retourne (recettes dans l'ordre demande, [(id, erreur), ...]).
        """
        uniques = list(dict.fromkeys(int(i) for i in item_ids))
        brutes: Dict[int, dict] = {}
        for debut in range(0, len(uniques), TAILLE_LOT):
            lot = uniques[debut:debut + TAILLE_LOT]
            params = {"resultId[$in][]": lot, "$limit": len(lot),
                      "$select[]": ["resultId", "ingredientIds", "quantities"]}
            for recette in self._get("/recipes", params).get("data", []):
                brutes.setdefault(recette["resultId"], recette)

        ids_ingredients = [i for r in brutes.values() for i in (r.get("ingredientIds") or [])]
        noms = self.noms_items(uniques + ids_ingredients)

        ok: List[Recette] = []
        echecs: List[tuple] = []
        for item_id in uniques:
            nom_item = noms.get(item_id) or "Item %s" % item_id
            brute = brutes.get(item_id)
            if brute is None:
                echecs.append((item_id, "Aucune recette pour %s" % nom_item))
                continue
            ids = brute.get("ingredientIds") or []
            quantites = brute.get("quantities") or []
            if len(ids) != len(quantites) or not ids:
                echecs.append((item_id, "Recette incoherente pour %s" % nom_item))
                continue
            ingredients = [Ingredient(i, noms.get(i) or "Item %s" % i, int(q))
                           for i, q in zip(ids, quantites)]
            recette = Recette(item_id=item_id, nom=nom_item, ingredients=ingredients)
            self._cache_recettes[normaliser(nom_item)] = recette
            ok.append(recette)
        LOG.info("Recettes par id : %d chargee(s), %d echec(s)", len(ok), len(echecs))
        return ok, echecs

    def recettes(self, noms: List[str]) -> tuple:
        """Charge plusieurs recettes. Retourne (recettes_ok, [(nom, erreur), ...])."""
        ok: List[Recette] = []
        echecs: List[tuple] = []
        for nom in noms:
            nom = (nom or "").strip()
            if not nom:
                continue
            try:
                ok.append(self.recette(nom))
            except ErreurAPI as exc:
                LOG.error("Echec chargement recette %r : %s", nom, exc)
                echecs.append((nom, str(exc)))
        return ok, echecs
