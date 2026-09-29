"""Selection de la zone de capture a la souris.

Affiche un voile semi-transparent couvrant TOUS les ecrans ; on trace un
rectangle sur le chat, et les coordonnees absolues sont enregistrees dans
config.json. A refaire apres un changement d'ecran ou de resolution.

Utilisable seul :  python selecteur_zone.py
ou depuis la fenetre principale (bouton "Zone").
"""
from __future__ import annotations

import logging
import tkinter as tk
from typing import List, Optional

LOG = logging.getLogger(__name__)

TAILLE_MINI = (60, 20)  # largeur, hauteur : en dessous c'est un clic parasite
CONSIGNE_DEFAUT = "Tracez un rectangle sur la zone de fil de commandes"


def bureau_virtuel() -> dict:
    """Rectangle englobant tous les ecrans (mss.monitors[0])."""
    import mss

    from ocr import _capteur_mss

    with _capteur_mss(mss)() as capteur:
        ecran = capteur.monitors[0]
    return {"left": ecran["left"], "top": ecran["top"],
            "width": ecran["width"], "height": ecran["height"]}


def ecrans() -> List[dict]:
    """Liste des ecrans, index 0 = bureau virtuel (convention mss)."""
    import mss

    from ocr import _capteur_mss

    with _capteur_mss(mss)() as capteur:
        return [dict(ecran) for ecran in capteur.monitors]


def monitor_contenant(zone: dict, liste_ecrans: List[dict]) -> int:
    """Index de l'ecran qui contient le centre de la zone (1 par defaut).

    mss.grab travaille en coordonnees absolues, mais la config garde le numero
    d'ecran pour que la validation au demarrage soit parlante.
    """
    centre_x = zone["left"] + zone["width"] / 2
    centre_y = zone["top"] + zone["height"] / 2
    for index, ecran in enumerate(liste_ecrans):
        if index == 0:
            continue  # bureau virtuel
        if (ecran["left"] <= centre_x < ecran["left"] + ecran["width"]
                and ecran["top"] <= centre_y < ecran["top"] + ecran["height"]):
            return index
    return 1


class SelecteurZone(tk.Toplevel):
    """Voile plein ecran : glisser-deposer pour definir la zone."""

    def __init__(self, parent: Optional[tk.Misc] = None,
                 zone_actuelle: Optional[dict] = None,
                 consigne: str = CONSIGNE_DEFAUT,
                 taille_mini: tuple = TAILLE_MINI) -> None:
        super().__init__(parent)
        self.zone: Optional[dict] = None
        self.taille_mini = taille_mini
        self._bureau = bureau_virtuel()
        self._depart = None
        self._rectangle = None

        self.overrideredirect(True)
        self.geometry("%dx%d+%d+%d" % (
            self._bureau["width"], self._bureau["height"],
            self._bureau["left"], self._bureau["top"]))
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.35)
        self.configure(bg="black")
        self.config(cursor="crosshair")

        self.toile = tk.Canvas(self, bg="black", highlightthickness=0, cursor="crosshair")
        self.toile.pack(fill="both", expand=True)

        self.toile.create_text(
            self._bureau["width"] // 2, 60,
            text=consigne + "\nEchap pour annuler",
            fill="white", font=("Segoe UI", 20, "bold"), justify="center",
        )
        # Signale un rectangle refuse : sinon rien ne se passe et on ne sait pas pourquoi.
        self._alerte = self.toile.create_text(
            self._bureau["width"] // 2, 140, text="", fill="#ffb4ae",
            font=("Segoe UI", 14, "bold"), justify="center")
        if zone_actuelle:
            # Repere de la zone actuellement configuree.
            self._dessiner_zone_actuelle(zone_actuelle)

        # Pas besoin d'exclure ce voile de la capture : l'appelant coupe
        # l'OCR avant d'ouvrir le selecteur (voir Serveur.cmd_zone), donc rien
        # ne peut le relire pendant qu'il est affiche.
        self.toile.bind("<ButtonPress-1>", self._debut)
        self.toile.bind("<B1-Motion>", self._glisse)
        self.toile.bind("<ButtonRelease-1>", self._fin)
        self.bind("<Escape>", lambda _evt: self.destroy())
        self.focus_force()
        self.grab_set()

    def _dessiner_zone_actuelle(self, zone: dict) -> None:
        x1 = zone["left"] - self._bureau["left"]
        y1 = zone["top"] - self._bureau["top"]
        self.toile.create_rectangle(
            x1, y1, x1 + zone["width"], y1 + zone["height"],
            outline="#888888", dash=(6, 4), width=2)
        self.toile.create_text(x1 + 4, y1 - 12, anchor="w", text="zone actuelle",
                               fill="#bbbbbb", font=("Segoe UI", 9))

    # ------------------------------------------------------------ evenements
    def _debut(self, evenement) -> None:
        self._depart = (evenement.x, evenement.y)
        if self._rectangle is not None:
            self.toile.delete(self._rectangle)
        self._rectangle = self.toile.create_rectangle(
            evenement.x, evenement.y, evenement.x, evenement.y,
            outline="#39d353", width=3, fill="#39d353", stipple="gray12")

    def _glisse(self, evenement) -> None:
        if self._depart is None:
            return
        self.toile.coords(self._rectangle, self._depart[0], self._depart[1],
                          evenement.x, evenement.y)

    def _fin(self, evenement) -> None:
        if self._depart is None:
            return
        x1, y1 = self._depart
        x2, y2 = evenement.x, evenement.y
        self._depart = None
        largeur, hauteur = abs(x2 - x1), abs(y2 - y1)
        if largeur < self.taille_mini[0] or hauteur < self.taille_mini[1]:
            LOG.info("Selection trop petite (%dx%d), ignoree", largeur, hauteur)
            if largeur or hauteur:  # simple clic : pas de message
                self.toile.itemconfigure(self._alerte, text=(
                    "Rectangle trop petit (%dx%d) : au moins %dx%d pixels, recommencez"
                    % (largeur, hauteur, self.taille_mini[0], self.taille_mini[1])))
            return  # on laisse l'utilisateur recommencer
        self.zone = {
            "left": int(min(x1, x2) + self._bureau["left"]),
            "top": int(min(y1, y2) + self._bureau["top"]),
            "width": int(largeur),
            "height": int(hauteur),
        }
        self.destroy()


def choisir_zone(parent: Optional[tk.Misc] = None,
                 zone_actuelle: Optional[dict] = None,
                 consigne: str = CONSIGNE_DEFAUT,
                 taille_mini: tuple = TAILLE_MINI) -> Optional[dict]:
    """Ouvre le selecteur et retourne la zone choisie (None si annule)."""
    autonome = parent is None
    racine = None
    if autonome:
        racine = tk.Tk()
        racine.withdraw()
        parent = racine
    selecteur = SelecteurZone(parent, zone_actuelle, consigne, taille_mini)
    parent.wait_window(selecteur)
    zone = selecteur.zone
    if racine is not None:
        racine.destroy()
    return zone


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import config as module_config

    configuration = module_config.charger()
    zone = choisir_zone(zone_actuelle=configuration["capture"]["zone"])
    if zone is None:
        print("Annule : config.json inchange.")
        return 1

    configuration["capture"]["zone"] = zone
    configuration["capture"]["monitor"] = monitor_contenant(zone, ecrans())
    module_config.sauvegarder(configuration)
    print("Zone enregistree dans config.json :", zone)
    print("Ecran :", configuration["capture"]["monitor"])
    print("Verifiez la lecture avec :  python outil_zone.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
