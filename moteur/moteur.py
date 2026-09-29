"""Colle entre le pipeline OCR et la session : parsing, dedup, comptage.

Tourne dans le thread consommateur OCR. Ne touche jamais a tkinter : l'UI lit
l'etat par sondage (Session est thread-safe).
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque, List, Optional

from ocr import ErreurOCR, PipelineOCR, verifier_tesseract
from parsing import DedupeurDeFlux, parser_capture, parser_message  # noqa: F401
from tracker import STATUT_A_CONFIRMER, STATUT_COMPTE, STATUT_REJETE, Session

LOG = logging.getLogger(__name__)


class Moteur:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.session: Optional[Session] = None
        self.pipeline: Optional[PipelineOCR] = None
        self.dedupeur = DedupeurDeFlux()
        self.journal: Deque[str] = deque(maxlen=200)
        self.derniere_erreur: Optional[str] = None
        self._verrou = threading.Lock()
        self.version_tesseract: Optional[str] = None

    # ------------------------------------------------------------ demarrage
    def verifier_environnement(self) -> None:
        ocr = self.config["ocr"]
        self.version_tesseract = verifier_tesseract(
            self.config.get("tesseract_cmd", ""),
            lang=ocr.get("lang", ""),
            tessdata_dir=ocr.get("tessdata_dir", ""),
        )
        LOG.info("Tesseract %s (langue %s)", self.version_tesseract, ocr.get("lang"))

    def nouvelle_session(self, recettes: List) -> Session:
        """Reset complet des compteurs et redemarrage du pipeline."""
        self.arreter()
        seuils = self.config["matching"]
        self.session = Session(
            recettes,
            seuil_auto=seuils["seuil_auto"],
            seuil_confirmation=seuils["seuil_confirmation"],
            taxe_pourcent=self.config.get("vente", {}).get("taxe_pourcent", 2.0),
        )
        self.dedupeur.reset()
        # Le chat affiche deja des achats d'avant la session : on les prend
        # comme reference sans les compter.
        self.dedupeur.amorcer()
        self.journal.clear()
        self.derniere_erreur = None
        self.pipeline = PipelineOCR(self.config, self._sur_texte)
        self.pipeline.demarrer()
        self._noter("Session demarree : %d recette(s), %d ingredient(s) suivis."
                    % (len(recettes), len(self.session.ingredients_actifs)))
        return self.session

    # -------------------------------------------------------- pause / reprise
    @property
    def en_pause(self) -> bool:
        return self.session is not None and self.pipeline is None

    def basculer_pause(self) -> bool:
        """Suspend ou relance la capture. Retourne True si en pause ensuite."""
        if self.session is None:
            return False
        if self.pipeline is None:
            self.reprendre()
            return False
        self.mettre_en_pause()
        return True

    def mettre_en_pause(self) -> None:
        """Arrete la capture en conservant tous les compteurs."""
        if self.pipeline is None:
            return
        self.arreter()
        self._noter("Capture en pause (compteurs conserves).")

    def reprendre(self) -> None:
        """Relance la capture sans compter ce qui s'est affiche pendant la pause.

        Les achats faits pendant la pause sont volontairement ignores : c'est
        le sens d'une pause. La premiere capture sert juste de reference.
        """
        if self.session is None or self.pipeline is not None:
            return
        self.dedupeur.amorcer()
        self.pipeline = PipelineOCR(self.config, self._sur_texte)
        self.pipeline.demarrer()
        self._noter("Capture reprise.")

    def redemarrer_pipeline(self) -> None:
        """Relance la capture (apres un changement de zone, par exemple).

        Les compteurs de la session sont conserves ; seul le flux OCR repart.
        """
        if self.session is None:
            return
        self.arreter()
        self.dedupeur.reset()
        self.dedupeur.amorcer()
        self.pipeline = PipelineOCR(self.config, self._sur_texte)
        self.pipeline.demarrer()
        capture = self.config["capture"]
        if capture.get("zone_rapide"):
            self._noter("Capture redemarree : zone rapide %s + zone de securite %s"
                        % (capture["zone_rapide"], capture["zone"]))
        else:
            self._noter("Capture redemarree sur la zone %s" % capture["zone"])

    def arreter(self) -> None:
        if self.pipeline is not None:
            try:
                self.pipeline.arreter()
            except Exception:  # pragma: no cover
                LOG.exception("Erreur a l'arret du pipeline")
            self.pipeline = None

    # -------------------------------------------------------------- callback
    def _sur_texte(self, texte: str, source: str = "principale") -> None:
        """Appele dans le(s) thread(s) OCR pour chaque capture lue.

        source "principale" (zone rapide, ou zone unique sans dual capture) :
        alimente le suivi positionnel normal (DedupeurDeFlux), qui determine
        ce qui est nouveau par sa POSITION dans le flux.

        source "securite" (zone de secours, optionnelle) : ne touche JAMAIS
        ce suivi positionnel. Une petite zone et une grande zone n'avancent
        pas a la meme vitesse ; les melanger dans le meme suivi positionnel
        deboussole l'algorithme (bug reel : des achats de plusieurs minutes
        plus tot ressurgissaient comme nouveaux). A la place, la zone de
        secours propose seulement le SURPLUS par rapport a ce qui a deja ete
        vu, compte ou propose (session.proposer_rattrapages, base sur une
        signature heure+quantite+prix+ingredient) : elle ne peut qu'ajouter
        ce qui manque, jamais recompter ce qui est deja acquis.
        """
        session = self.session
        if session is None:
            return
        try:
            messages = parser_capture(texte)
            if not messages:
                return
            if source == "securite":
                achats = [a for a in map(parser_message, messages) if a is not None]
                if not achats:
                    return
                with self._verrou:
                    comptes, a_confirmer = session.proposer_rattrapages(achats)
                if comptes:
                    self._noter("%d achat(s) rattrape(s) automatiquement par la zone de securite."
                               % comptes)
                if a_confirmer:
                    self._noter("%d achat(s) rattrape(s) par la zone de securite : a confirmer."
                               % a_confirmer)
                return
            with self._verrou:
                nouveaux = self.dedupeur.nouveaux(messages)
                douteux = list(self.dedupeur.ecartes_douteux)
            for message in nouveaux:
                achat = parser_message(message)
                if achat is None:
                    continue  # ventes, messages banque, bruit : ignores
                resultat = session.traiter_achat(achat)
                if resultat.statut == STATUT_COMPTE:
                    self._noter("[%s] %d x %s -> %s (%d k)" % (
                        achat.heure, achat.quantite, achat.nom_item,
                        resultat.ingredient, achat.prix_total))
                elif resultat.statut == STATUT_A_CONFIRMER:
                    self._noter("[%s] A CONFIRMER : %r ~ %r (%.0f%%)" % (
                        achat.heure, achat.nom_item, resultat.ingredient, resultat.score))
                elif resultat.statut == STATUT_REJETE:
                    self._noter("[%s] NON IDENTIFIE : %r (%.0f%%)" % (
                        achat.heure, achat.nom_item, resultat.score))
            achats_douteux = [a for a in map(parser_message, douteux) if a is not None]
            if achats_douteux:
                comptes, a_confirmer = session.proposer_rattrapages(achats_douteux)
                if comptes:
                    self._noter("%d achat(s) relu(s) apres une perte d'alignement, rattrapes automatiquement."
                               % comptes)
                if a_confirmer:
                    self._noter("Achat(s) relu(s) apres une perte d'alignement : a confirmer.")
        except Exception as exc:  # jamais de crash silencieux du thread OCR
            self.derniere_erreur = str(exc)
            LOG.exception("Erreur de traitement du texte OCR")

    def _noter(self, ligne: str) -> None:
        self.journal.append(ligne)
        LOG.info(ligne)

    # ------------------------------------------------------------------ etat
    @property
    def taille_queue(self) -> int:
        return self.pipeline.taille_queue if self.pipeline else 0

    def sante_pipeline(self) -> str:
        """Ligne d'etat compacte affichee en continu dans l'UI."""
        if self.pipeline is None:
            return "capture en PAUSE" if self.session else "pipeline : arrete"
        return "queue OCR : %d/%d  |  ocr %.0f ms  |  jetees %d  |  resync %d" % (
            self.pipeline.taille_queue,
            self.pipeline.file.maxsize,
            self.pipeline.duree_ocr_ms,
            self.pipeline.captures_perdues,
            self.dedupeur.resynchronisations,
        )

    @property
    def pipeline_actif(self) -> bool:
        return bool(self.pipeline and self.pipeline.actif)

    def alerte_saturation(self) -> Optional[str]:
        """Message d'alerte si le chat visible a ete sature par une rafale.

        Dans ce cas des achats ont pu ne pas etre comptes, sans que rien ne
        permette de le savoir depuis l'image (voir DedupeurDeFlux).
        """
        if not self.dedupeur.saturation_active:
            return None
        achat = parser_message(self.dedupeur.message_sature or "")
        cible = achat.nom_item if achat else "la ressource concernee"
        return ("Chat sature par un achat repete (%s) : tant qu'il n'affiche que "
                "cette ligne, une rafale qui continue serait invisible. Verifiez "
                "ce total (+n) et agrandissez le fil de commandes." % cible)

    def erreur_courante(self) -> Optional[str]:
        if self.derniere_erreur:
            return self.derniere_erreur
        if self.pipeline is not None:
            return self.pipeline.derniere_erreur
        return None
