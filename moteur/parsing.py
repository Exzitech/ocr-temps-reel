"""Parsing du texte OCR du fil de commandes du fournisseur.

Deux points cles :

1. On ne decoupe JAMAIS sur les retours a la ligne : un nom d'item long est
   visuellement coupe sur deux lignes par le client. On decoupe sur le pattern
   d'horodatage, chaque [hh:mm] marquant le debut d'un nouveau message, puis on
   reconstitue le message complet avant de le parser.

2. La deduplication entre deux captures successives se fait par ORDRE/POSITION
   dans le flux, jamais par contenu : deux achats consecutifs peuvent produire
   un texte rigoureusement identique et sont pourtant deux transactions
   distinctes. Voir DedupeurDeFlux.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from rapidfuzz import fuzz

LOG = logging.getLogger(__name__)

# Debut de message : [hh:mm]
RE_HORODATAGE = re.compile(r"\[\d{2}:\d{2}\]")

# Le logiciel du fournisseur separe les milliers : "(1 067 euros)", "(2 299 euros)". Le separateur
# peut etre une espace ordinaire, insecable ou fine, et l'OCR le rend parfois
# par un point ou une virgule. On accepte tout ca et on ne garde que les
# chiffres. Sans cela, TOUT achat a 1000 euros ou plus passait a la trappe.
NOMBRE = r"[\d\s  .,]*\d"

# Message d'achat : [hh:mm] 12 x [Nom de l'item] (3 400 euros)
RE_ACHAT = re.compile(
    r"\[(\d{2}:\d{2})\]\s*(" + NOMBRE + r")\s*x\s*\[(.+?)\]"
    r"\s*\(\s*(" + NOMBRE + r")\s*euros?\s*\)",
    re.IGNORECASE | re.DOTALL,
)

# Le grossiste ne vend que par lots de 1, 10, 100 ou 1000. Toute autre quantite est
# une erreur de lecture : un chiffre parasite colle a l'horodatage ("[21:521 1 x"
# lu "71 x") gonflait le cumul de 70 unites par achat. Bug reel observe.
LOTS_GROSSISTE = frozenset({1, 10, 100, 1000})

RE_ESPACES = re.compile(r"\s+")
RE_NON_CHIFFRE = re.compile(r"\D")


def entier(texte: str) -> int:
    """'1 067' -> 1067. Tolere les separateurs de milliers de toutes formes."""
    chiffres = RE_NON_CHIFFRE.sub("", texte or "")
    return int(chiffres) if chiffres else 0


@dataclass(frozen=True)
class Achat:
    heure: str
    quantite: int
    nom_item: str
    prix_total: int

    @property
    def cle(self) -> str:
        """Cle de comparaison positionnelle (jamais utilisee comme cle d'unicite)."""
        return "%s|%d|%s|%d" % (self.heure, self.quantite, self.nom_item, self.prix_total)


RE_MINUTE = re.compile(r"^\[(\d{2}):(\d{2})\]")


def _minute(message: str) -> Optional[int]:
    """'[23:28] ...' -> 1408 (minutes depuis minuit). None si illisible."""
    correspondance = RE_MINUTE.match(message)
    if not correspondance:
        return None
    heures, minutes = int(correspondance.group(1)), int(correspondance.group(2))
    if heures > 23 or minutes > 59:
        return None
    return heures * 60 + minutes


def decouper_messages(texte_ocr: str) -> List[str]:
    """Decoupe le texte OCR en messages complets, un par horodatage."""
    if not texte_ocr:
        return []
    texte = texte_ocr.replace("\r", "\n")
    debuts = [m.start() for m in RE_HORODATAGE.finditer(texte)]
    messages = []
    for indice, debut in enumerate(debuts):
        fin = debuts[indice + 1] if indice + 1 < len(debuts) else len(texte)
        # Les sauts de ligne internes sont des coupures visuelles : on les recolle.
        message = RE_ESPACES.sub(" ", texte[debut:fin]).strip()
        if message:
            messages.append(message)
    return messages


def parser_message(message: str) -> Optional[Achat]:
    """Extrait un achat d'un message. None si ce n'est pas un achat.

    Les lignes de vente / banque ("Banque : + X Euros (vente...)") ne matchent
    pas le pattern et sont ignorees naturellement.
    """
    correspondance = RE_ACHAT.search(message)
    if not correspondance:
        return None
    heure, quantite, nom_item, prix = correspondance.groups()
    nom_item = RE_ESPACES.sub(" ", nom_item).strip(" -,.")
    quantite, prix = entier(quantite), entier(prix)
    if not nom_item or quantite <= 0 or prix <= 0:
        LOG.debug("Message d'achat incomplet, ignore : %r", message)
        return None
    if quantite not in LOTS_GROSSISTE:
        # Ecarte plutot que compte faux : la zone rapide relit le meme achat,
        # et un achat manquant se corrige au bouton +n.
        LOG.warning("Quantite %d hors lots du grossiste, achat ecarte : %r", quantite, message)
        return None
    return Achat(heure=heure, quantite=quantite, nom_item=nom_item, prix_total=prix)


def parser_capture(texte_ocr: str) -> List[str]:
    """Texte OCR brut -> liste ordonnee des messages du chat."""
    return decouper_messages(texte_ocr)


class DedupeurDeFlux:
    """Deduplication positionnelle entre captures successives.

    Le chat est une liste ordonnee qui defile. Deux captures consecutives se
    recouvrent largement. On cherche le plus long recouvrement entre la fin de
    ce qu'on a deja accepte et le debut de la nouvelle capture ; tout ce qui
    suit ce recouvrement est nouveau.

    Consequence voulue : trois achats identiques d'affilee donnent trois
    messages a la suite dans la capture, donc trois transactions comptees.

    Deux garde-fous, appris d'une session reelle en service :

    - l'alignement TOLERE des ecarts a l'interieur du recouvrement. Une seule
      ligne mal lue au milieu de la fenetre suffisait sinon a faire echouer
      l'alignement entier ;
    - quand l'alignement echoue quand meme, on NE COMPTE RIEN et on se
      resynchronise. Compter toute la fenetre (comportement precedent)
      recomptait jusqu'a 30 achats deja enregistres a chaque hoquet de l'OCR.
      Mieux vaut manquer quelques achats, corrigeables au bouton +n, que de
      gonfler silencieusement les totaux.

    L'historique garde TOUJOURS au moins une capture entiere. Bug reel : un
    chat agrandi affichait 62 messages pour 60 memorises ; le debut de chaque
    capture n'etait donc jamais retrouve et TOUTES les captures passaient en
    resynchronisation (935 dans la soiree), perdant les achats de la minute.
    """

    def __init__(self, taille_historique: int = 60, seuil_similarite: int = 88,
                 tolerance_alignement: float = 0.7) -> None:
        self.taille_historique = taille_historique
        self.seuil_similarite = seuil_similarite
        self.tolerance_alignement = tolerance_alignement
        self._historique: List[str] = []
        self.resynchronisations = 0
        self.derniere_resync: Optional[List[str]] = None
        self.captures_saturees = 0
        self.message_sature: Optional[str] = None
        self.saturation_active = False
        self._taille_precedente = 0
        self._amorcage = False
        # Minute la plus recente jamais lue : ne recule jamais pendant la session.
        self._minute_max: Optional[int] = None
        # Plus grande capture vue : l'historique n'est jamais plus court.
        self._taille_fenetre = 0
        # Achats de la derniere minute ecartes par la derniere resynchronisation :
        # peut-etre deja comptes, peut-etre pas. Le moteur les propose a confirmer.
        self.ecartes_douteux: List[str] = []

    def amorcer(self) -> None:
        """La prochaine capture sert de reference SANS rien compter.

        Utilise au demarrage d'une session et a la reprise apres une pause :
        le chat affiche deja des achats anterieurs, qui ne doivent pas venir
        gonfler les compteurs.
        """
        self._historique = []
        self._amorcage = True

    def reset(self) -> None:
        self._historique = []
        self.resynchronisations = 0
        self.derniere_resync = None
        self.captures_saturees = 0
        self.message_sature = None
        self.saturation_active = False
        self._taille_precedente = 0
        self._minute_max = None
        self._taille_fenetre = 0
        self.ecartes_douteux = []

    def _detecter_saturation(self, messages: List[str], nb_nouveaux: int) -> None:
        """Signale une fenetre saturee par un seul et meme message.

        LIMITE DE FOND : quand tout le chat visible est occupe par des achats
        rigoureusement identiques (meme item, meme prix, meme minute), deux
        captures successives sont identiques au pixel pres, que le chat ait
        defile ou non. Aucun traitement d'image ne peut lever l'ambiguite :
        l'information n'est pas dans la capture.

        Le risque n'existe que si la fenetre est PLEINE et n'evolue plus : tant
        qu'elle se remplit encore (sa taille augmente, ou on y detecte du
        nouveau), chaque achat reste distinguable par sa position et rien n'est
        perdu. On ne crie donc que dans le cas reellement ambigu.

        Parade : agrandir le fil de commandes pour qu'il affiche au moins autant de
        lignes que la plus grosse rafale d'achats.
        """
        taille = len(messages)
        self._taille_precedente = taille
        if taille < 3:
            self.saturation_active = False
            return
        identiques = all(self._identiques(messages[0], autre) for autre in messages[1:])
        # Tant que la fenetre se remplit, on detecte du nouveau et chaque achat
        # reste distinguable par sa position : aucun risque. L'angle mort
        # commence quand la fenetre n'est plus QUE ce message et que plus rien
        # n'y bouge - etat ou une rafale qui continue serait invisible.
        ambigu = identiques and nb_nouveaux == 0
        self.saturation_active = ambigu
        if ambigu:
            self.captures_saturees += 1
            self.message_sature = messages[0]
            if self.captures_saturees == 1 or self.captures_saturees % 20 == 0:
                LOG.warning(
                    "Fenetre saturee (%d messages identiques, %d captures) : des "
                    "achats peuvent ne pas etre comptes. Agrandissez le "
                    "fil de commandes. Message : %s", taille, self.captures_saturees, messages[0],
                )

    @property
    def historique(self) -> List[str]:
        return list(self._historique)

    @staticmethod
    def _cle(message: str) -> str:
        """Forme comparable : casse et ponctuation parasite neutralisees."""
        return RE_ESPACES.sub(" ", message).strip().lower()

    def _identiques(self, a: str, b: str) -> bool:
        cle_a, cle_b = self._cle(a), self._cle(b)
        if cle_a == cle_b:
            return True
        # Tolerance au bruit OCR : la meme ligne peut varier de quelques signes.
        return fuzz.ratio(cle_a, cle_b) >= self.seuil_similarite

    def _aligne(self, gauche: List[str], droite: List[str]) -> bool:
        """Vrai si les deux sequences decrivent les memes messages.

        On accepte quelques positions divergentes : sur une fenetre de 20
        messages, une ligne bruitee ne doit pas invalider l'alignement.
        """
        if not gauche:
            return False
        concordances = sum(
            1 for a, b in zip(gauche, droite) if self._identiques(a, b))
        if len(gauche) == 1:
            return concordances == 1
        # Les extremites ancrent l'alignement : elles doivent concorder.
        if not self._identiques(gauche[-1], droite[-1]):
            return False
        return concordances / len(gauche) >= self.tolerance_alignement

    def nouveaux(self, messages: List[str]) -> List[str]:
        """Retourne les messages absents de la capture precedente, dans l'ordre."""
        resultat = self._nouveaux(messages)
        self._memoriser_minutes(messages)
        return resultat

    def _nouveaux(self, messages: List[str]) -> List[str]:
        self.ecartes_douteux = []
        if not messages:
            return []
        self._taille_fenetre = max(self._taille_fenetre, len(messages))
        if self._amorcage:
            self._amorcage = False
            self._detecter_saturation(messages, 0)
            self._enregistrer(messages)
            LOG.info("Amorcage : %d message(s) deja affiches, non comptes",
                     len(messages))
            return []
        if not self._historique:
            self._detecter_saturation(messages, len(messages))
            self._enregistrer(messages)
            return list(messages)

        recouvrement_max = min(len(self._historique), len(messages))
        for taille in range(recouvrement_max, 0, -1):
            if self._aligne(self._historique[-taille:], messages[:taille]):
                nouveaux = messages[taille:]
                self._detecter_saturation(messages, len(nouveaux))
                self._enregistrer(nouveaux)
                return nouveaux
        ancre = self._ancre(messages)
        if ancre is not None:
            nouveaux = messages[ancre + 1:]
            LOG.info("Alignement par ancre (dernier message connu en position %d/%d) : "
                     "%d nouveau(x)", ancre + 1, len(messages), len(nouveaux))
            self._detecter_saturation(messages, len(nouveaux))
            self._enregistrer(nouveaux)
            return nouveaux
        # Plus aucun point commun : on a perdu le fil (chat vide, onglet change,
        # zone deplacee, OCR parti en vrille). On repart de cette fenetre sans
        # recompter ce qui y est encore affiche - sauf ce qui est strictement
        # posterieur a tout ce qu'on a deja lu, qui ne peut pas avoir ete compte.
        recents = self._posterieurs_a_l_historique(messages)
        self._detecter_saturation(messages, len(recents))
        self.resynchronisations += 1
        self.derniere_resync = list(messages)
        LOG.warning(
            "Resynchronisation (%d au total) : aucun recouvrement avec les %d "
            "messages connus, %d messages ignores pour ne rien recompter, %d "
            "posterieurs comptes", self.resynchronisations, len(self._historique),
            len(messages) - len(recents), len(recents),
        )
        for message in messages[:5]:
            LOG.warning("  capture non alignee : %s", message)
        if self._minute_max is not None:
            ecartes = messages[:len(messages) - len(recents)]
            self.ecartes_douteux = [m for m in ecartes if _minute(m) == self._minute_max]
        self._historique = list(messages)[-self._limite():]
        return recents

    def _ancre(self, messages: List[str]) -> Optional[int]:
        """Position du dernier message connu, s'il apparait UNE seule fois.

        Bug reel (Champignon de Paris) : fenetre de 3 messages dont une ligne
        mal lue ; sur si peu de messages, l'alignement tolerant echouait et
        l'achat suivant, de la meme minute, etait ecarte. Si le dernier message
        compte est present une seule fois dans la capture, tout ce qui le suit
        est forcement nouveau. Present plusieurs fois (rafale identique) :
        ambigu, on ne s'en sert pas.
        """
        if not self._historique:
            return None
        dernier = self._historique[-1]
        positions = [i for i, m in enumerate(messages) if self._identiques(dernier, m)]
        return positions[0] if len(positions) == 1 else None

    def _limite(self) -> int:
        return max(self.taille_historique, self._taille_fenetre)

    def _posterieurs_a_l_historique(self, messages: List[str]) -> List[str]:
        """Fin de fenetre dont l'horodatage depasse STRICTEMENT tout ce qui a ete lu.

        Bug reel : le premier achat d'une rafale est arrive seul dans une
        capture sans recouvrement ; la resynchronisation l'ignorait.

        Prudence : meme minute = peut-etre deja compte, donc exclu. Le passage
        de minuit (00:01 < 23:59) n'est pas vu comme posterieur : exclu aussi.
        On ne prend qu'une fin de fenetre continue, le chat etant chronologique.

        On compare a la minute la plus recente JAMAIS lue, pas a l'historique :
        celui-ci est remplace a chaque resynchronisation (chat remonte, onglet
        change), et revenir ensuite au chat courant recompterait tout ce qui
        est posterieur au vieux contenu.
        """
        if self._minute_max is None:
            return []
        debut = len(messages)
        while debut > 0:
            minute = _minute(messages[debut - 1])
            if minute is None or minute <= self._minute_max:
                break
            debut -= 1
        return list(messages[debut:])

    def _memoriser_minutes(self, messages: List[str]) -> None:
        minutes = [m for m in map(_minute, messages) if m is not None]
        if minutes:
            self._minute_max = max(minutes + ([self._minute_max]
                                              if self._minute_max is not None else []))

    def _enregistrer(self, messages: List[str]) -> None:
        if not messages:
            return
        self._historique.extend(messages)
        if len(self._historique) > self._limite():
            self._historique = self._historique[-self._limite():]
