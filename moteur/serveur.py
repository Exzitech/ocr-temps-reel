"""Coût des plats - backend sans fenetre pilote par ExziHub, en WebSocket.

Le moteur, la session, l'OCR et catalogue de recettes sont
les memes ; seul l'affichage change de processus.

  GET  http://localhost:8000/health  -> 200 (voyant de la sidebar du hub)
  WS   ws://localhost:8000/ws

Serveur -> hub (chaque message a un champ "type") :
  session_state   {session: {...} | null}      instantane complet, a chaque changement
  queue_status    {taille, max, ocr_ms, ...}   sante du pipeline OCR
  backend_status  {etat, erreur, alerte, ...}  bandeau d'etat, journal
  recipe_frozen   {recette, ingredient, prix_fige, manuel}
  session_reset   {recettes, echecs}
  command_result  {id, ok, message}            reponse a une commande

Hub -> serveur : {"type": <commande>, "id": <optionnel>, ...} (voir COMMANDES).

Tout est renvoye a la connexion : un hub qui se reconnecte retrouve l'etat
sans rien demander. L'etat est sonde (comme le faisait l'UI tkinter) et
n'est diffuse que s'il a change.

Lancement : pythonw serveur.py (ExziHub le fait via hub/tools.json).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import sys
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from typing import Callable, Dict, List, Optional, Set, Tuple

import config as module_config

LOG = logging.getLogger("serveur")

PERIODE_S = 0.25
LIGNES_JOURNAL = 40


class ErreurCommande(Exception):
    """Commande refusee : le message est affiche tel quel dans le hub."""


def enregistrer_dans_config(modifications: dict,
                            chemin: str = module_config.CHEMIN_CONFIG) -> None:
    """Ecrit seulement ces cles dans config.json, tel qu'il est sur disque.

    Sauvegarder la config chargee en memoire y figerait ses valeurs resolues
    (tessdata_dir relatif devenu absolu, cles par defaut ajoutees) : le dossier
    ne serait plus deplacable.
    """
    with open(chemin, "r", encoding="utf-8-sig") as fichier:
        brut = json.load(fichier)

    def fusionner(cible: dict, source: dict) -> None:
        for cle, valeur in source.items():
            if isinstance(valeur, dict) and isinstance(cible.get(cle), dict):
                fusionner(cible[cle], valeur)
            else:
                cible[cle] = valeur

    fusionner(brut, modifications)
    module_config.sauvegarder(brut, chemin)


# ---------------------------------------------------------------- instantanes
def instantane_session(moteur) -> Optional[dict]:
    """Tout ce que l'UI tkinter affichait, en donnees JSON."""
    session = moteur.session if moteur else None
    if session is None:
        return None

    ids = {r.nom: r.item_id for r in session.recettes}
    recettes = []
    for etat in session.etat():
        recettes.append({
            "nom": etat.nom,
            "item_id": ids.get(etat.nom),
            "nb_plats": etat.nb_plats,
            "cout_total": etat.cout_total,
            "cout_unitaire": etat.cout_unitaire,
            "ingredients": [{
                "nom": i.nom,
                "quantite_requise": i.quantite_requise,
                "quantite_unitaire": i.quantite_unitaire,
                "quantite_cumulee": i.quantite_cumulee,
                "euros_cumules": i.euros_cumules,
                "prix_moyen": i.prix_moyen,
                "prix_fige": i.prix_fige,
                "fige_manuellement": i.fige_manuellement,
                "quantite_stock": i.quantite_stock,
            } for i in etat.ingredients],
        })

    corrections = {}
    for nom in session.ingredients_actifs:
        liste = session.corrections_de(nom)
        if liste:
            corrections[nom] = [{"identifiant": c.identifiant, "libelle": c.libelle}
                                for c in liste]

    return {
        "recettes": recettes,
        "ingredients_actifs": list(session.ingredients_actifs),
        "corrections": corrections,
        "rattrapes": session.rattrapes(),
        "en_attente": [{
            "identifiant": a.identifiant,
            "heure": a.achat.heure,
            "quantite": a.achat.quantite,
            "nom_item": a.achat.nom_item,
            "prix_total": a.achat.prix_total,
            "ingredient_propose": a.ingredient_propose,
            "score": a.score,
            "rattrape": a.rattrape,
        } for a in session.achats_en_attente()],
        "non_identifies": session.resume_non_identifies(),
        "liste_courses": session.liste_courses(),
        "recap": session.recapitulatif(),
        "taxe_pourcent": session.taux_taxe * 100,
        "nb_achats": session.nb_achats_comptes,
        "en_pause": moteur.en_pause,
    }


def statut_queue(moteur) -> dict:
    pipeline = moteur.pipeline if moteur else None
    if pipeline is None:
        return {"actif": False, "taille": 0, "max": 0, "ocr_ms": 0,
                "jetees": 0, "resync": 0}
    return {
        "actif": pipeline.actif,
        "taille": pipeline.taille_queue,
        "max": pipeline.file.maxsize + pipeline.file_securite.maxsize,
        "ocr_ms": round(pipeline.duree_ocr_ms),
        "ocr_ms_securite": round(pipeline.duree_ocr_ms_securite) if pipeline.boite_securite else None,
        "hors_jeu": pipeline.hors_jeu,
        "jetees": pipeline.captures_perdues,
        "resync": moteur.dedupeur.resynchronisations,
    }


# ------------------------------------------------------------------- serveur
class Serveur:
    def __init__(self, configuration: Optional[dict], moteur,
                 erreur_demarrage: Optional[str] = None) -> None:
        self.config = configuration
        self.moteur = moteur
        # Erreur qui empeche tout (config illisible) ou avertissement (Tesseract).
        self.erreur_demarrage = erreur_demarrage
        self.avertissement: Optional[str] = None
        self.chargement = False
        self.derniers_items: List[str] = list(
            (configuration or {}).get("session", {}).get("items_par_defaut", []))
        self.plats_memorises: List[dict] = [
            c for c in (configuration or {}).get("session", {}).get("plats_memorises", [])
            if isinstance(c, dict) and isinstance(c.get("id"), int)]
        self.clients: Set = set()
        self._evenements: deque = deque()
        self._derniers: Dict[str, str] = {}
        self._figes: Set[Tuple[str, str]] = set()
        self._session_suivie = None
        # Une seule session chargee a la fois (appels catalogue de recettes de plusieurs s).
        self._verrou_session = threading.Lock()
        # Tkinter (selecteur de zone) : toujours depuis le meme thread, et une
        # seule racine Tk reutilisee (voir _racine_tk).
        self._thread_tk = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tk")
        self._racine_tk = None

        self.commandes: Dict[str, Callable[[dict], Optional[str]]] = {
            "new_session": self.cmd_nouvelle_session,
            "toggle_pause": self.cmd_pause,
            "select_zone": self.cmd_zone,
            "select_zone_rapide": self.cmd_zone_rapide,
            "clear_zone_rapide": self.cmd_retirer_zone_rapide,
            "set_capture_settings": self.cmd_reglages_capture,
            "undo_rattrape": self.cmd_annuler_rattrape,
            "manual_increment": self.cmd_increment,
            "set_plats": self.cmd_plats,
            "adjust_cumul": self.cmd_cumul,
            "set_frozen_price": self.cmd_prix_fige,
            "unfreeze": self.cmd_defiger,
            "undo_correction": self.cmd_annuler_correction,
            "confirm_purchase": self.cmd_confirmer,
            "reject_purchase": self.cmd_rejeter,
            "link_unknown": self.cmd_lier,
            "forget_unknown": self.cmd_oublier,
            "set_sale_price": self.cmd_prix_vente,
            "set_stock": self.cmd_stock,
            "add_items": self.cmd_ajouter_items,
            "remove_recipe": self.cmd_retirer_recette,
            "clear_queue": self.cmd_vider_queue,
            "set_unit_price": self.cmd_prix_unitaire,
            "plat_done": self.cmd_cuisine,
            "remember_plat": self.cmd_memoriser_plat,
            "forget_plat": self.cmd_oublier_plat,
            "restore_plats": self.cmd_restaurer_plats,
        }

    # ------------------------------------------------------------- messages
    def statut_backend(self) -> dict:
        moteur = self.moteur
        if moteur is None:
            etat = "erreur"
        elif moteur.session is None:
            etat = "sans_session"
        elif moteur.en_pause:
            etat = "pause"
        else:
            etat = "actif" if moteur.pipeline_actif else "arrete"
        max_items = (self.config or {}).get("session", {}).get("max_items", 20)
        return {
            "etat": etat,
            "chargement": self.chargement,
            "erreur": self.erreur_demarrage or (moteur.erreur_courante() if moteur else None),
            "avertissement": self.avertissement,
            "alerte": moteur.alerte_saturation() if moteur else None,
            "tesseract": moteur.version_tesseract if moteur else None,
            "zone": (self.config or {}).get("capture", {}).get("zone"),
            "zone_rapide": (self.config or {}).get("capture", {}).get("zone_rapide"),
            "reglages": self.reglages_capture(),
            "journal": list(moteur.journal)[-LIGNES_JOURNAL:] if moteur else [],
            "derniers_items": self.derniers_items,
            "plats_memorises": self.plats_memorises,
            "max_items": max_items,
        }

    def messages_etat(self) -> List[dict]:
        """Les trois messages d'etat, plus les evenements survenus depuis."""
        session = instantane_session(self.moteur)
        messages = [
            {"type": "session_state", "session": session},
            {"type": "queue_status", **statut_queue(self.moteur)},
            {"type": "backend_status", **self.statut_backend()},
        ]
        self._detecter_figeages(session)
        while self._evenements:
            messages.append(self._evenements.popleft())
        return messages

    def _detecter_figeages(self, session: Optional[dict]) -> None:
        courante = self.moteur.session if self.moteur else None
        figes = set()
        manuels = {}
        for recette in (session or {}).get("recettes", []):
            for ing in recette["ingredients"]:
                if ing["prix_fige"] is not None:
                    cle = (recette["nom"], ing["nom"])
                    figes.add(cle)
                    manuels[cle] = (ing["prix_fige"], ing["fige_manuellement"])
        if courante is self._session_suivie:
            for recette, ingredient in sorted(figes - self._figes):
                prix, manuel = manuels[(recette, ingredient)]
                self._evenements.append({"type": "recipe_frozen", "recette": recette,
                                         "ingredient": ingredient, "prix_fige": prix,
                                         "manuel": manuel})
        self._session_suivie = courante
        self._figes = figes

    # ------------------------------------------------------------- commandes
    def _session(self):
        if self.moteur is None:
            raise ErreurCommande(self.erreur_demarrage or "Moteur indisponible")
        if self.moteur.session is None:
            raise ErreurCommande("Aucune session active : lance une nouvelle session.")
        return self.moteur.session

    @staticmethod
    def _texte(message: dict, cle: str) -> str:
        valeur = message.get(cle)
        if not isinstance(valeur, str) or not valeur.strip():
            raise ErreurCommande("Champ %r manquant" % cle)
        return valeur

    @staticmethod
    def _nombre(message: dict, cle: str, mini: float = 0, entier: bool = True,
                optionnel: bool = False):
        valeur = message.get(cle)
        if valeur is None and optionnel:
            return None
        if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
            raise ErreurCommande("Champ %r : nombre attendu" % cle)
        if entier and valeur != int(valeur):
            raise ErreurCommande("Champ %r : entier attendu" % cle)
        if valeur < mini:
            raise ErreurCommande("Champ %r : minimum %s" % (cle, mini))
        return int(valeur) if entier else float(valeur)

    def _ingredient(self, session, message: dict) -> str:
        nom = self._texte(message, "ingredient")
        if nom not in session.quantite_cumulee:
            raise ErreurCommande("Ingredient hors session : %s" % nom)
        return nom

    def _recette(self, session, message: dict) -> str:
        nom = self._texte(message, "recette")
        if nom not in session.nb_plats:
            raise ErreurCommande("Recette hors session : %s" % nom)
        return nom

    def cmd_nouvelle_session(self, message: dict) -> str:
        from catalogue import ClientCatalogue, ErreurAPI
        from ocr import ErreurOCR

        if self.moteur is None:
            raise ErreurCommande(self.erreur_demarrage or "Moteur indisponible")
        items = message.get("items")
        if not isinstance(items, list):
            raise ErreurCommande("Champ 'items' : liste attendue")
        items = [str(i).strip() for i in items if str(i).strip()]
        max_items = self.statut_backend()["max_items"]
        if not items:
            raise ErreurCommande("Indiquez au moins un item.")
        if len(items) > max_items:
            raise ErreurCommande("%d items maximum." % max_items)
        if not self._verrou_session.acquire(blocking=False):
            raise ErreurCommande("Une session est deja en cours de chargement.")
        try:
            self.chargement = True
            try:
                recettes, echecs = ClientCatalogue().recettes(items)
            except ErreurAPI as exc:
                raise ErreurCommande(str(exc)) from exc
            finally:
                self.chargement = False
            if not recettes:
                raise ErreurCommande("Aucune recette chargee : " +
                                     " ; ".join("%s (%s)" % e for e in echecs))
            try:
                self.moteur.nouvelle_session(recettes)
            except ErreurOCR as exc:
                raise ErreurCommande(str(exc)) from exc
        finally:
            self._verrou_session.release()

        self.derniers_items = items
        self._memoriser_items(items)
        self._evenements.append({"type": "session_reset",
                                 "recettes": [r.nom for r in recettes],
                                 "echecs": [{"nom": n, "erreur": e} for n, e in echecs]})
        texte = "Session demarree : %d recette(s)." % len(recettes)
        if echecs:
            texte += " Non chargees : " + ", ".join(n for n, _ in echecs)
        return texte

    def cmd_ajouter_items(self, message: dict) -> str:
        """Ajoute des items du catalogue (par id) a la session, sans reset.

        Sans session active, en demarre une avec ces items.
        items : [{"id": 744, "quantite": 2}, ...]
        """
        from catalogue import ClientCatalogue, ErreurAPI
        from ocr import ErreurOCR

        if self.moteur is None:
            raise ErreurCommande(self.erreur_demarrage or "Moteur indisponible")
        demandes = message.get("items")
        if not isinstance(demandes, list) or not demandes:
            raise ErreurCommande("Champ 'items' : liste non vide attendue")
        quantites: Dict[int, int] = {}
        for demande in demandes:
            if not isinstance(demande, dict):
                raise ErreurCommande("Item invalide : %r" % demande)
            item_id = self._nombre(demande, "id", mini=1)
            quantites[item_id] = min(9999, self._nombre(demande, "quantite", mini=1,
                                                        optionnel=True) or 1)

        if not self._verrou_session.acquire(blocking=False):
            raise ErreurCommande("Une session est deja en cours de chargement.")
        try:
            session = self.moteur.session
            deja = set(session.nb_plats) if session else set()
            max_items = self.statut_backend()["max_items"]
            self.chargement = True
            try:
                recettes, echecs = ClientCatalogue().recettes_par_ids(list(quantites))
            except ErreurAPI as exc:
                raise ErreurCommande(str(exc)) from exc
            finally:
                self.chargement = False
            nouvelles = [r for r in recettes if r.nom not in deja]
            if not nouvelles:
                detail = " ; ".join(e for _, e in echecs) or "deja dans la session"
                raise ErreurCommande("Rien a ajouter : %s." % detail)
            if len(deja) + len(nouvelles) > max_items:
                raise ErreurCommande("%d recettes maximum (%d deja dans la session)."
                                     % (max_items, len(deja)))
            plats = {r.nom: quantites[r.item_id] for r in nouvelles}
            if session is None:
                try:
                    session = self.moteur.nouvelle_session(nouvelles)
                except ErreurOCR as exc:
                    raise ErreurCommande(str(exc)) from exc
                for nom, nombre in plats.items():
                    session.definir_nb_plats(nom, nombre)
                self._evenements.append({"type": "session_reset",
                                         "recettes": [r.nom for r in nouvelles],
                                         "echecs": [{"nom": str(i), "erreur": e}
                                                    for i, e in echecs]})
            else:
                session.ajouter_recettes(nouvelles, plats)
        finally:
            self._verrou_session.release()

        self._memoriser_session()
        texte = "Ajoute : " + ", ".join(
            "%s x%d" % (r.nom, plats[r.nom]) if plats[r.nom] > 1 else r.nom
            for r in nouvelles)
        if echecs:
            texte += ". Sans recette : " + ", ".join(e for _, e in echecs)
        return texte

    def cmd_retirer_recette(self, message: dict) -> str:
        session = self._session()
        recette = self._recette(session, message)
        session.retirer_recette(recette)
        self._memoriser_session()
        return "%s retire de la session." % recette

    def cmd_cuisine(self, message: dict) -> str:
        session = self._session()
        recette = self._recette(session, message)
        nombre = message.get("nombre")
        if nombre is not None:
            nombre = min(9999, self._nombre(message, "nombre", mini=1))
        session.cuisiner(recette)
        if nombre is not None:
            session.definir_nb_plats(recette, nombre)
        return "%s reset : ingredients consommes, x%d a cuisiner." % (
            recette, session.nb_plats.get(recette, 1))

    # ------------------------------------------------------ plats memorises
    def cmd_memoriser_plat(self, message: dict) -> str:
        """Met un plat de cote pour une prochaine session (quantite, prix de vente)."""
        session = self._session()
        nom = self._recette(session, message)
        recette = next(r for r in session.recettes if r.nom == nom)
        plat = {"id": recette.item_id, "nom": nom,
                 "quantite": session.nb_plats.get(nom, 1),
                 "prix_vente": session.prix_vente.get(nom)}
        autres = [c for c in self.plats_memorises if c["id"] != recette.item_id]
        self._enregistrer_plats(autres + [plat])
        return "%s x%d garde pour plus tard." % (nom, plat["quantite"])

    def cmd_oublier_plat(self, message: dict) -> None:
        item_id = self._nombre(message, "id", mini=1)
        self._enregistrer_plats([c for c in self.plats_memorises if c["id"] != item_id])

    def cmd_restaurer_plats(self, message: dict) -> str:
        """Ajoute des plats memorises a la session (ou la demarre), puis les retire de la memoire."""
        ids = message.get("ids")
        if not isinstance(ids, list) or not ids:
            raise ErreurCommande("Champ 'ids' : liste non vide attendue")
        choisis = [c for c in self.plats_memorises if c["id"] in ids]
        if not choisis:
            raise ErreurCommande("Plat memorise introuvable.")
        texte = self.cmd_ajouter_items({"items": [
            {"id": c["id"], "quantite": c.get("quantite") or 1} for c in choisis]})
        session = self.moteur.session
        presents = set(session.nb_plats) if session else set()
        for plat in choisis:
            if plat["nom"] in presents and plat.get("prix_vente"):
                session.definir_prix_vente(plat["nom"], plat["prix_vente"])
        self._enregistrer_plats([c for c in self.plats_memorises
                                  if not (c in choisis and c["nom"] in presents)])
        return texte

    def _enregistrer_plats(self, plats: List[dict]) -> None:
        self.plats_memorises = plats
        self.config.setdefault("session", {})["plats_memorises"] = plats
        try:
            enregistrer_dans_config({"session": {"plats_memorises": plats}})
        except Exception as exc:
            raise ErreurCommande("Plats memorises non enregistres : %s" % exc) from exc

    def _memoriser_session(self) -> None:
        session = self.moteur.session if self.moteur else None
        if session is not None:
            self.derniers_items = [r.nom for r in session.recettes]
            self._memoriser_items(self.derniers_items)

    def _memoriser_items(self, items: List[str]) -> None:
        """Pre-remplit la prochaine session, meme apres un redemarrage."""
        self.config["session"]["items_par_defaut"] = items
        try:
            enregistrer_dans_config({"session": {"items_par_defaut": items}})
        except Exception as exc:  # confort seulement
            LOG.warning("Items non memorises dans config.json : %s", exc)

    def cmd_vider_queue(self, _message: dict) -> str:
        pipeline = self.moteur.pipeline if self.moteur else None
        if pipeline is None:
            raise ErreurCommande("La capture n'est pas active.")
        jetees = pipeline.vider()
        return "Queue OCR videe (%d capture(s) jetee(s))." % jetees

    def cmd_pause(self, _message: dict) -> str:
        from ocr import ErreurOCR

        self._session()
        try:
            en_pause = self.moteur.basculer_pause()
        except ErreurOCR as exc:
            raise ErreurCommande(str(exc)) from exc
        return "Capture en pause : les achats ne sont pas comptes." if en_pause \
            else "Capture reprise."

    def cmd_zone(self, _message: dict) -> Optional[str]:
        return self._appliquer_zone("zone", "Trace un rectangle sur la zone de fil de commandes")

    def cmd_zone_rapide(self, _message: dict) -> Optional[str]:
        """Petite zone (dernieres lignes) lue tres souvent pour un retour immediat.

        Une fois configuree, la zone habituelle (« zone ») devient un filet de
        securite relu moins souvent (capture.interval_securite_s) pour
        rattraper une rafale d'achats trop rapide pour la petite zone seule.
        """
        return self._appliquer_zone(
            "zone_rapide",
            "Trace un PETIT rectangle sur les 2-3 dernieres lignes du chat.\n"
            "Lu en direct, en plus de la zone habituelle gardee en filet de securite.")

    def _appliquer_zone(self, cle: str, consigne: str) -> Optional[str]:
        if self.moteur is None:
            raise ErreurCommande(self.erreur_demarrage or "Moteur indisponible")
        # Capture coupee pendant le trace, par prudence : le pipeline ne doit
        # pas tourner pendant que le voile plein ecran (exclu de la capture
        # pour ne pas se relire) est affiche par-dessus. Le vrai plantage au
        # recadrage venait d'ailleurs (voir _racine) mais ceci reste plus sur.
        self.moteur.arreter()
        try:
            # Voile plein ecran tkinter : bloque jusqu'au trace ou a Echap.
            zone, monitor = self._thread_tk.submit(self._choisir_zone, cle, consigne).result()
        except Exception:
            self.moteur.redemarrer_pipeline()  # reprend sur l'ancienne configuration
            raise
        if zone is None:
            self.moteur.redemarrer_pipeline()  # annule : reprend sur l'ancienne configuration
            return None
        capture = self.config["capture"]
        capture[cle] = zone
        capture["monitor"] = monitor
        try:
            enregistrer_dans_config({"capture": {cle: zone, "monitor": monitor}})
        except (module_config.ErreurConfig, OSError, ValueError) as exc:
            self.moteur.redemarrer_pipeline()  # reprend sur l'ancienne configuration : mieux que rien
            raise ErreurCommande("Zone appliquee mais non enregistree : %s" % exc) from exc
        try:
            self.moteur.redemarrer_pipeline()
        except Exception as exc:
            raise ErreurCommande("Zone invalide : %s" % exc) from exc
        return "Zone : %dx%d en (%d, %d)" % (zone["width"], zone["height"],
                                             zone["left"], zone["top"])

    def cmd_retirer_zone_rapide(self, _message: dict) -> str:
        """Retour a une seule zone (comportement d'origine)."""
        if self.moteur is None:
            raise ErreurCommande(self.erreur_demarrage or "Moteur indisponible")
        if not self.config["capture"].get("zone_rapide"):
            raise ErreurCommande("Aucune zone rapide a retirer.")
        self.moteur.arreter()
        self.config["capture"]["zone_rapide"] = None
        try:
            enregistrer_dans_config({"capture": {"zone_rapide": None}})
        except (module_config.ErreurConfig, OSError, ValueError) as exc:
            self.moteur.redemarrer_pipeline()
            raise ErreurCommande("Zone rapide retiree mais non enregistree : %s" % exc) from exc
        self.moteur.redemarrer_pipeline()
        return "Zone rapide retiree : retour a une seule zone."

    # Bornes des reglages modifiables depuis le hub (cle -> (section, mini, maxi, entier)).
    REGLAGES_CAPTURE = {
        "interval_s": ("capture", 0.05, 2.0, False),
        "interval_securite_s": ("capture", 0.3, 10.0, False),
        "upscale": ("ocr", 1, 3, True),
    }

    def reglages_capture(self) -> dict:
        """Valeurs courantes des reglages de capture, avec leurs defauts."""
        cfg = self.config or {}
        capture, ocr = cfg.get("capture", {}), cfg.get("ocr", {})
        return {
            "interval_s": float(capture.get("interval_s", 0.25)),
            "interval_securite_s": float(capture.get("interval_securite_s", 2.0)),
            "upscale": int(ocr.get("upscale", 2)),
            "pause_hors_jeu": bool(capture.get("pause_hors_jeu", True)),
        }

    def cmd_reglages_capture(self, message: dict) -> str:
        """Modifie les reglages de capture (intervalles, upscale, pause hors du logiciel du fournisseur)."""
        if self.moteur is None:
            raise ErreurCommande(self.erreur_demarrage or "Moteur indisponible")
        modifications: Dict[str, dict] = {}
        for cle, (section, mini, maxi, entier) in self.REGLAGES_CAPTURE.items():
            if message.get(cle) is None:
                continue
            valeur = self._nombre(message, cle, mini=mini, entier=entier)
            if valeur > maxi:
                raise ErreurCommande("Champ %r : maximum %s" % (cle, maxi))
            modifications.setdefault(section, {})[cle] = valeur
        if "pause_hors_jeu" in message:
            if not isinstance(message["pause_hors_jeu"], bool):
                raise ErreurCommande("Champ 'pause_hors_jeu' : booleen attendu")
            modifications.setdefault("capture", {})["pause_hors_jeu"] = message["pause_hors_jeu"]
        if not modifications:
            raise ErreurCommande("Aucun reglage a modifier.")
        for section, valeurs in modifications.items():
            self.config.setdefault(section, {}).update(valeurs)
        try:
            enregistrer_dans_config(modifications)
        except (module_config.ErreurConfig, OSError, ValueError) as exc:
            raise ErreurCommande("Reglages appliques mais non enregistres : %s" % exc) from exc
        self.moteur.redemarrer_pipeline()
        return "Reglages de capture mis a jour."

    def cmd_annuler_rattrape(self, message: dict) -> str:
        session = self._session()
        if not session.annuler_rattrape(self._nombre(message, "identifiant", mini=1)):
            raise ErreurCommande("Rattrapage introuvable ou deja consomme par un plat.")
        return "Rattrapage annule."

    def _racine(self):
        """Racine Tk cachee, creee une seule fois et reutilisee.

        Bug reel observe en service : creer puis detruire un tk.Tk() a chaque
        recadrage plante Tcl au 2e appel (STATUT_BREAKPOINT dans tcl86t.dll,
        toujours au meme endroit - panique interne de Tcl a la creation d'un
        nouvel interpreteur dans un processus qui en a deja eu un). Une seule
        racine geree pendant toute la vie du serveur : seul le Toplevel de
        selection est recree a chaque fois, ce que Tkinter supporte bien.
        """
        if self._racine_tk is None:
            import tkinter as tk

            self._racine_tk = tk.Tk()
            self._racine_tk.withdraw()
        return self._racine_tk

    def _choisir_zone(self, cle: str, consigne: str):
        from selecteur_zone import choisir_zone, ecrans, monitor_contenant

        try:
            zone = choisir_zone(parent=self._racine(), zone_actuelle=self.config["capture"].get(cle),
                                consigne=consigne)
            return zone, (monitor_contenant(zone, ecrans()) if zone else None)
        except Exception as exc:
            LOG.exception("Selecteur de zone indisponible")
            raise ErreurCommande("Selecteur indisponible : %s" % exc) from exc

    def cmd_increment(self, message: dict) -> Optional[str]:
        session = self._session()
        ingredient = self._ingredient(session, message)
        quantite = self._nombre(message, "quantite", mini=1)
        euros = self._nombre(message, "euros", mini=0, optionnel=True)
        session.correction_manuelle(ingredient, quantite, euros or None)
        return None  # silencieux : l'etat suit, pas de toast en rafale

    def cmd_plats(self, message: dict) -> None:
        session = self._session()
        recette = self._recette(session, message)
        nombre = min(9999, self._nombre(message, "nombre", mini=1))
        session.definir_nb_plats(recette, nombre)

    def cmd_cumul(self, message: dict) -> str:
        session = self._session()
        ingredient = self._ingredient(session, message)
        if not session.ajuster_cumul(ingredient, self._nombre(message, "quantite"),
                                     self._nombre(message, "euros")):
            raise ErreurCommande("Cumul refuse.")
        return "Cumul de %s corrige." % ingredient

    def cmd_prix_fige(self, message: dict) -> str:
        session = self._session()
        recette = self._recette(session, message)
        ingredient = self._ingredient(session, message)
        prix = self._nombre(message, "prix", entier=False)
        if not session.definir_prix_fige(recette, ingredient, prix):
            raise ErreurCommande("Prix refuse.")
        return "Prix impose : %s / %s." % (recette, ingredient)

    def cmd_prix_unitaire(self, message: dict) -> str:
        session = self._session()
        ingredient = self._ingredient(session, message)
        prix = self._nombre(message, "prix", entier=False, optionnel=True)
        if prix is not None and prix <= 0:
            prix = None
        nombre = session.definir_prix_unitaire(ingredient, prix)
        if prix is None:
            return "%s : prix rendu au calcul automatique." % ingredient
        return "%s : %.0f k/u impose (%d recette(s))." % (ingredient, prix, nombre)

    def cmd_defiger(self, message: dict) -> str:
        session = self._session()
        recette = self._recette(session, message)
        ingredient = self._ingredient(session, message)
        session.defiger(recette, ingredient)
        return "%s / %s rendu au figeage automatique." % (recette, ingredient)

    def cmd_annuler_correction(self, message: dict) -> str:
        session = self._session()
        if not session.annuler_correction(self._nombre(message, "identifiant", mini=1)):
            raise ErreurCommande("Correction introuvable (deja annulee ?).")
        return "Correction annulee."

    def cmd_confirmer(self, message: dict) -> str:
        session = self._session()
        cible = message.get("ingredient")
        if cible is not None:
            cible = self._ingredient(session, message)
        identifiant = self._nombre(message, "identifiant", mini=1)
        attente = session.en_attente.get(identifiant)
        if attente is None or not session.confirmer(identifiant, cible):
            raise ErreurCommande("Achat deja traite.")
        return "Achat confirme : %d x %s (%d k)." % (
            attente.achat.quantite, cible or attente.ingredient_propose, attente.achat.prix_total)

    def cmd_rejeter(self, message: dict) -> str:
        session = self._session()
        identifiant = self._nombre(message, "identifiant", mini=1)
        attente = session.en_attente.get(identifiant)
        if attente is None or not session.rejeter(identifiant):
            raise ErreurCommande("Achat deja traite.")
        return "Achat ecarte : %d x %s." % (attente.achat.quantite, attente.achat.nom_item)

    def cmd_lier(self, message: dict) -> str:
        session = self._session()
        nom_ocr = self._texte(message, "nom_ocr")
        ingredient = self._ingredient(session, message)
        recuperes = session.lier(nom_ocr, ingredient)
        return ("%d achat(s) de « %s » rattaches a %s. Les suivants le seront "
                "automatiquement." % (recuperes, nom_ocr, ingredient))

    def cmd_oublier(self, message: dict) -> None:
        self._session().oublier_non_identifies(self._texte(message, "nom_ocr"))

    def cmd_stock(self, message: dict) -> None:
        session = self._session()
        ingredient = self._ingredient(session, message)
        session.definir_stock(ingredient, self._nombre(message, "quantite"))

    def cmd_prix_vente(self, message: dict) -> None:
        session = self._session()
        recette = self._recette(session, message)
        prix = self._nombre(message, "prix", entier=False, optionnel=True)
        session.definir_prix_vente(recette, prix)

    # --------------------------------------------------------------- reseau
    async def _envoyer(self, connexion, messages: List[dict]) -> None:
        try:
            for message in messages:
                await connexion.send(json.dumps(message, ensure_ascii=False))
        except Exception:  # client parti : sa boucle de reception le retirera
            pass

    async def _diffuser(self, forcer_pour=None) -> None:
        messages = self.messages_etat()
        nouveaux = []
        for message in messages:
            texte = json.dumps(message, ensure_ascii=False, sort_keys=True)
            cle = message["type"]
            if cle in ("session_state", "queue_status", "backend_status"):
                if self._derniers.get(cle) == texte:
                    continue
                self._derniers[cle] = texte
            nouveaux.append(message)
        if forcer_pour is not None:
            etats = {m["type"]: m for m in messages}
            envoi = [etats["session_state"], etats["queue_status"], etats["backend_status"]]
            await self._envoyer(forcer_pour, envoi)
        if nouveaux:
            await asyncio.gather(*(self._envoyer(c, nouveaux) for c in list(self.clients)))

    async def boucle_diffusion(self) -> None:
        while True:
            try:
                await self._diffuser()
            except Exception:  # pragma: no cover - la diffusion ne doit jamais mourir
                LOG.exception("Erreur de diffusion")
            await asyncio.sleep(PERIODE_S)

    async def traiter_connexion(self, connexion) -> None:
        self.clients.add(connexion)
        LOG.info("Hub connecte (%d client(s))", len(self.clients))
        try:
            await self._diffuser(forcer_pour=connexion)
            async for brut in connexion:
                await self._traiter_message(connexion, brut)
        except Exception as exc:
            LOG.info("Connexion fermee : %s", exc)
        finally:
            self.clients.discard(connexion)
            LOG.info("Hub deconnecte (%d client(s))", len(self.clients))

    async def _traiter_message(self, connexion, brut) -> None:
        try:
            message = json.loads(brut)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return
        identifiant = message.get("id")
        commande = self.commandes.get(message["type"])
        if commande is None:
            resultat = {"ok": False, "message": "Commande inconnue : %s" % message["type"]}
        else:
            # Chaque commande dans un thread : une session qui charge ou un
            # trace de zone ne bloque ni les +1 ni la diffusion.
            asyncio.create_task(self._executer(connexion, identifiant, commande, message))
            return
        await self._envoyer(connexion, [{"type": "command_result", "id": identifiant,
                                         **resultat}])

    async def _executer(self, connexion, identifiant, commande, message) -> None:
        try:
            texte = await asyncio.to_thread(commande, message)
            resultat = {"ok": True, "message": texte}
        except ErreurCommande as exc:
            resultat = {"ok": False, "message": str(exc)}
        except Exception as exc:
            LOG.exception("Commande %s en erreur", message.get("type"))
            resultat = {"ok": False, "message": "Erreur inattendue : %s" % exc}
        await self._envoyer(connexion, [{"type": "command_result", "id": identifiant,
                                         **resultat}])
        await self._diffuser()  # l'effet s'affiche sans attendre le prochain tick


# Sans hub (fermeture forcee) ce backend resterait orphelin : avec --quitter-sans-hub,
# il s'arrete quand plus rien n'a ete demande (ping /health du hub, toutes les 4 s).
DELAI_SANS_HUB_S = 90.0
_dernier_signe = time.monotonic()


def _surveiller_hub_sync() -> None:
    while True:
        time.sleep(10)
        if time.monotonic() - _dernier_signe > DELAI_SANS_HUB_S:
            LOG.warning("Hub absent depuis %d s : arret du backend.", DELAI_SANS_HUB_S)
            os._exit(0)


def reponse_http(connexion, requete):
    global _dernier_signe
    _dernier_signe = time.monotonic()
    """/health pour le voyant du hub ; seul /ws passe au handshake WebSocket."""
    chemin = requete.path.split("?")[0]
    if chemin == "/health":
        return connexion.respond(HTTPStatus.OK, "OK\n")
    if chemin != "/ws":
        return connexion.respond(HTTPStatus.NOT_FOUND, "Not found\n")
    return None


async def servir(serveur: Serveur, hote: str, port: int) -> None:
    from websockets.asyncio.server import serve

    if "--quitter-sans-hub" in sys.argv:
        threading.Thread(target=_surveiller_hub_sync, daemon=True, name="veille-hub").start()

    async with serve(serveur.traiter_connexion, hote, port,
                     process_request=reponse_http, max_size=2 ** 20):
        LOG.info("Serveur pret : ws://%s:%d/ws", hote, port)
        await serveur.boucle_diffusion()


def main() -> int:
    from journal import FICHIER_DEFAUT, configurer_logs

    configuration = None
    moteur = None
    erreur = None
    try:
        configuration = module_config.charger()
    except module_config.ErreurConfig as exc:
        erreur = str(exc)
        configurer_logs({"log": {"fichier": FICHIER_DEFAUT}})
    if configuration is not None:
        configurer_logs(configuration)
    # Le ping /health du hub (toutes les 4 s) noierait le journal.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    LOG.info("Demarrage du serveur Coût des plats")

    serveur_conf = (configuration or module_config.CONFIG_DEFAUT).get("serveur", {})
    hote = serveur_conf.get("hote", "localhost")
    port = int(serveur_conf.get("port", 8000))

    serveur = None
    if configuration is not None:
        from moteur import Moteur
        from ocr import ErreurOCR

        moteur = Moteur(configuration)
        serveur = Serveur(configuration, moteur)
        try:
            moteur.verifier_environnement()
        except ErreurOCR as exc:
            # Non fatal : le hub affiche le probleme au lieu d'un backend muet.
            serveur.avertissement = str(exc)
            LOG.error("%s", exc)
    else:
        LOG.error("%s", erreur)
        serveur = Serveur(None, None, erreur_demarrage=erreur)

    try:
        asyncio.run(servir(serveur, hote, port))
    except OSError as exc:
        LOG.error("Port %d indisponible (serveur deja lance ?) : %s", port, exc)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        if moteur is not None:
            moteur.arreter()
        LOG.info("Arret du serveur Coût des plats")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
