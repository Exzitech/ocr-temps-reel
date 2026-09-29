"""Chargement / sauvegarde de la configuration (config.json).

La config est volontairement externe au code : la zone de capture depend de la
resolution d'ecran, elle doit pouvoir changer sans toucher au code.
"""
from __future__ import annotations

import copy
import json
import logging
import os

from donnees import chemin_donnees

LOG = logging.getLogger(__name__)

# Dans le dossier de donnees, hors de plugins/<id>/ que chaque mise a jour
# efface. "tessdata" suit automatiquement : _resoudre_chemins resout les
# chemins relatifs par rapport au dossier du config.json.
CHEMIN_CONFIG = chemin_donnees("config.json")

WHITELIST_DEFAUT = (
    "0123456789"
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "àâäçèéêëîïôö"
    "ùûüœæ"
    "ÀÂÄÇÈÉÊËÎÏÔÖ"
    "ÙÛÜŒÆ"
    "[]()x:'’ -,.+"
)

CONFIG_DEFAUT = {
    "_commentaire": (
        "zone = pixels absolus du fil de commandes (onglet filtre messages systeme). "
        "Modifiable sans toucher au code."
    ),
    "tesseract_cmd": "",
    "capture": {
        "monitor": 1,
        "zone": {"left": 20, "top": 780, "width": 900, "height": 260},
        # L'OCR coute ~300 ms par image : capturer plus vite ne sert qu'a
        # accumuler du retard. La file est courte et jette les images perimees.
        "interval_s": 0.4,
        "queue_maxsize": 4,
    },
    "ocr": {
        "lang": "fra",
        "psm": 6,
        "whitelist": WHITELIST_DEFAUT,
        "upscale": 2,
        # Dossier contenant <lang>.traineddata. Vide = installation Tesseract par
        # defaut. Utile quand on n'a pas les droits sur C:\Program Files.
        "tessdata_dir": "",
    },
    "matching": {"seuil_auto": 90, "seuil_confirmation": 70},
    # Commission de vente (plateforme, livraison), en % du PRIX DE VENTE.
    "vente": {"taxe_pourcent": 2.0},
    # plats_memorises : plats mis de cote pour une prochaine session
    # ({id, nom, quantite, prix_vente}), survivent aux redemarrages.
    "session": {"max_items": 20, "items_par_defaut": [], "plats_memorises": []},
    "ui": {"refresh_ms": 300, "topmost": True},
    # Backend du hub (serveur.py) : WebSocket ws://hote:port/ws.
    "serveur": {"hote": "localhost", "port": 8000},
    "log": {"fichier": "cout-plats.log", "niveau": "INFO"},
}


class ErreurConfig(Exception):
    """Config illisible ou invalide."""


def _fusionne(defaut: dict, charge: dict) -> dict:
    """Fusion recursive : une config partielle reste valide."""
    resultat = copy.deepcopy(defaut)
    for cle, valeur in charge.items():
        if isinstance(valeur, dict) and isinstance(resultat.get(cle), dict):
            resultat[cle] = _fusionne(resultat[cle], valeur)
        else:
            resultat[cle] = valeur
    return resultat


def charger(chemin: str = CHEMIN_CONFIG) -> dict:
    if not os.path.exists(chemin):
        LOG.warning("config.json absent, creation avec les valeurs par defaut : %s", chemin)
        # Completee avant d'etre ecrite : c'est le premier lancement, celui ou
        # l'utilisateur a le plus besoin que Tesseract soit trouve tout seul.
        config = copy.deepcopy(CONFIG_DEFAUT)
        _completer_installation(config, os.path.dirname(os.path.abspath(chemin)))
        sauvegarder(config, chemin)
        return config
    try:
        # utf-8-sig : tolere le BOM que le Bloc-notes et PowerShell ajoutent
        # volontiers. Sans ca, un fichier edite a la main devient illisible.
        with open(chemin, "r", encoding="utf-8-sig") as fichier:
            charge = json.load(fichier)
    except (OSError, json.JSONDecodeError) as exc:
        raise ErreurConfig("config.json illisible (%s) : %s" % (chemin, exc)) from exc
    if not isinstance(charge, dict):
        raise ErreurConfig("config.json doit contenir un objet JSON")
    config = _fusionne(CONFIG_DEFAUT, charge)
    racine = os.path.dirname(os.path.abspath(chemin))
    _resoudre_chemins(config, racine)
    _completer_installation(config, racine)
    _valider(config)
    return config


# Emplacements d'installation habituels de Tesseract sous Windows, dans l'ordre
# ou installer.ps1 les essaie lui-meme.
INSTALLATIONS_TESSERACT = (
    r"%ProgramFiles%\Tesseract-OCR\tesseract.exe",
    r"%ProgramFiles(x86)%\Tesseract-OCR\tesseract.exe",
    r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe",
)


def trouver_tesseract() -> str:
    """Binaire Tesseract sur cette machine. Vide s'il est deja dans le PATH."""
    import shutil

    if shutil.which("tesseract"):
        return ""  # pytesseract le trouvera seul
    for motif in INSTALLATIONS_TESSERACT:
        chemin = os.path.expandvars(motif)
        if "%" not in chemin and os.path.isfile(chemin):
            return chemin
    return ""


def _resoudre_chemins(config: dict, racine: str) -> None:
    """Rend les chemins relatifs independants du repertoire courant.

    "tessdata" doit marcher que l'outil soit lance depuis son dossier ou
    ailleurs (raccourci, tache planifiee...).
    """
    dossier = config["ocr"].get("tessdata_dir", "")
    if dossier and not os.path.isabs(dossier):
        config["ocr"]["tessdata_dir"] = os.path.normpath(os.path.join(racine, dossier))


def _completer_installation(config: dict, racine: str) -> None:
    """Complete ce qui depend de la machine, que l'utilisateur n'a pas a saisir.

    L'installeur ne peut plus rien ecrire ici : quand il tourne, le plugin n'est
    pas encore installe. Le moteur trouve donc Tesseract lui-meme, et prend le
    pack de langue depose a cote de sa configuration s'il y en a un (le cas
    quand l'installeur n'avait pas les droits sur Program Files).
    """
    if not config.get("tesseract_cmd"):
        config["tesseract_cmd"] = trouver_tesseract()
    if not config["ocr"].get("tessdata_dir"):
        local = os.path.join(racine, "tessdata")
        if os.path.isdir(local):
            config["ocr"]["tessdata_dir"] = local


def _valider(config: dict) -> None:
    zone = config["capture"]["zone"]
    for cle in ("left", "top", "width", "height"):
        if not isinstance(zone.get(cle), int):
            raise ErreurConfig("capture.zone.%s doit etre un entier (voir config.json)" % cle)
    if zone["width"] <= 0 or zone["height"] <= 0:
        raise ErreurConfig("capture.zone : width et height doivent etre > 0")
    if config["capture"]["interval_s"] <= 0:
        raise ErreurConfig("capture.interval_s doit etre > 0")
    seuils = config["matching"]
    if not 0 < seuils["seuil_confirmation"] <= seuils["seuil_auto"] <= 100:
        raise ErreurConfig("matching : 0 < seuil_confirmation <= seuil_auto <= 100")


def sauvegarder(config: dict, chemin: str = CHEMIN_CONFIG) -> None:
    try:
        with open(chemin, "w", encoding="utf-8") as fichier:
            json.dump(config, fichier, ensure_ascii=False, indent=2)
            fichier.write("\n")
    except OSError as exc:
        raise ErreurConfig("Impossible d'ecrire %s : %s" % (chemin, exc)) from exc
