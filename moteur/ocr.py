"""Pipeline producteur / consommateur : capture d'ecran -> OCR -> texte.

Thread producteur : capture en boucle la zone du chat via mss et empile chaque
screenshot dans une queue.Queue thread-safe.
Thread consommateur : depile, fait l'OCR avec pytesseract et transmet le texte
au callback fourni.

La taille de la queue est un indicateur de sante du pipeline : si elle grossit,
l'OCR n'arrive plus a suivre la capture.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from donnees import DOSSIER_DONNEES, chemin_donnees

LOG = logging.getLogger(__name__)


class ErreurOCR(Exception):
    """Tesseract absent, mal configure, ou zone d'ecran introuvable."""


@dataclass
class Capture:
    horodatage: float
    image: object  # PIL.Image.Image


WDA_EXCLUDEFROMCAPTURE = 0x00000011


def exclure_de_la_capture(identifiant_tk: int) -> bool:
    """Rend une fenetre de l'outil invisible a sa propre capture d'ecran.

    Bug reel observe en service : les fenetres de l'outil sont "toujours au-dessus"
    et la console affichait le journal. Posees sur la zone du chat, elles
    etaient relues par l'OCR : chaque ligne de journal "[22:36] 10 x [...]"
    redevenait un achat, qui produisait une nouvelle ligne de journal... Un
    seul achat reel avait ete compte 9 fois, et un faux prix fige.

    Windows 10 (2004+) et 11 savent exclure une fenetre des captures : la
    capture voit alors ce qu'il y a DERRIERE elle, c'est-a-dire le logiciel du fournisseur.
    Retourne False si le systeme ne le permet pas.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        GA_ROOT = 2
        fenetre = user32.GetAncestor(identifiant_tk, GA_ROOT)
        if not fenetre:
            return False
        return bool(user32.SetWindowDisplayAffinity(fenetre, WDA_EXCLUDEFROMCAPTURE))
    except Exception as exc:  # pragma: no cover - API absente (Windows ancien)
        LOG.warning("Exclusion de la capture impossible : %s", exc)
        return False


def _capteur_mss(module):
    """Classe de capture mss : MSS depuis la 10.x, mss() avant (deprecie)."""
    return getattr(module, "MSS", None) or module.mss


def verifier_tesseract(chemin_binaire: str = "", lang: str = "",
                       tessdata_dir: str = "") -> str:
    """Verifie que Tesseract est utilisable (et la langue disponible). Version."""
    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover
        raise ErreurOCR("Module pytesseract absent : pip install -r requirements.txt") from exc
    if chemin_binaire:
        pytesseract.pytesseract.tesseract_cmd = chemin_binaire
    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:  # pytesseract.TesseractNotFoundError et derives
        raise ErreurOCR(
            "Tesseract introuvable. Installez-le puis renseignez \"tesseract_cmd\" "
            "dans config.json (chemin complet vers tesseract.exe). Detail : %s" % exc
        ) from exc

    if lang:
        if tessdata_dir and not os.path.isdir(tessdata_dir):
            raise ErreurOCR("ocr.tessdata_dir introuvable : %s" % tessdata_dir)
        try:
            disponibles = pytesseract.get_languages(config=_option_tessdata(tessdata_dir))
        except Exception as exc:
            LOG.warning("Impossible de lister les langues Tesseract : %s", exc)
            return version
        if lang not in disponibles:
            raise ErreurOCR(
                "Langue OCR %r indisponible (presentes : %s). Telechargez %s.traineddata "
                "depuis github.com/tesseract-ocr/tessdata et placez-le dans le dossier "
                "indique par ocr.tessdata_dir dans config.json."
                % (lang, ", ".join(disponibles) or "aucune", lang)
            )
    return version


def _chemin_pour_config(chemin: str) -> str:
    """Rend un chemin passable dans la chaine de config de pytesseract.

    Deux contraintes, dues aux deux appels shlex de pytesseract :
      - posix=True (get_languages) mange les backslashes non quotes ;
      - posix=False (image_to_string sous Windows) conserve les guillemets.
    Donc : pas de guillemets, pas de backslash (slashes, acceptes par Tesseract
    sous Windows), et pas d'espace (chemin court 8.3).
    """
    if not chemin:
        return chemin
    if " " not in chemin or os.name != "nt":
        return chemin.replace("\\", "/")
    try:
        import ctypes

        tampon = ctypes.create_unicode_buffer(1024)
        longueur = ctypes.windll.kernel32.GetShortPathNameW(chemin, tampon, 1024)
        if longueur and " " not in tampon.value:
            return tampon.value.replace("\\", "/")
    except Exception as exc:  # pragma: no cover
        LOG.warning("Conversion en chemin court impossible (%s) : %s", chemin, exc)
    LOG.warning("Chemin contenant un espace non convertible : %s", chemin)
    return chemin.replace("\\", "/")


def _option_tessdata(tessdata_dir: str) -> str:
    """Option ligne de commande pour un dossier tessdata hors installation."""
    if not tessdata_dir:
        return ""
    return "--tessdata-dir %s" % _chemin_pour_config(tessdata_dir)


def ecrire_fichier_whitelist(whitelist: str, dossier: str) -> str:
    """Ecrit la whitelist dans un fichier de config Tesseract et retourne son chemin.

    Passer la whitelist en ligne de commande est impossible : elle contient une
    espace (les noms d'items en ont). Tesseract sait lire un fichier de config
    "variable valeur", ou la valeur est tout le reste de la ligne.
    """
    chemin = os.path.join(dossier, "whitelist.tessconfig")
    try:
        with open(chemin, "w", encoding="utf-8") as fichier:
            fichier.write("tessedit_char_whitelist %s\n" % whitelist)
    except OSError as exc:
        raise ErreurOCR("Impossible d'ecrire %s : %s" % (chemin, exc)) from exc
    return _chemin_pour_config(chemin)


def verifier_zone(config_capture: dict, cle: str = "zone") -> dict:
    """Valide une zone de capture (config_capture[cle]) contre les ecrans reellement presents."""
    try:
        import mss
    except ImportError as exc:  # pragma: no cover
        raise ErreurOCR("Module mss absent : pip install -r requirements.txt") from exc

    numero = config_capture.get("monitor", 1)
    zone = config_capture[cle]
    with _capteur_mss(mss)() as capteur:
        if numero >= len(capteur.monitors):
            raise ErreurOCR(
                "Ecran %s introuvable (%d ecran(s) detecte(s)). Corrigez capture.monitor "
                "dans config.json." % (numero, len(capteur.monitors) - 1)
            )
        ecran = capteur.monitors[numero]

    boite = {
        "left": zone["left"],
        "top": zone["top"],
        "width": zone["width"],
        "height": zone["height"],
    }
    hors_ecran = (
        boite["left"] + boite["width"] <= ecran["left"]
        or boite["top"] + boite["height"] <= ecran["top"]
        or boite["left"] >= ecran["left"] + ecran["width"]
        or boite["top"] >= ecran["top"] + ecran["height"]
    )
    if hors_ecran:
        raise ErreurOCR(
            "La zone de capture %s est entierement hors de l'ecran %s. "
            "Corrigez capture.%s dans config.json." % (boite, ecran, cle)
        )
    return boite


def construire_config_tesseract(reglages_ocr: dict, dossier_travail: str = "") -> str:
    """Chaine d'options passee a pytesseract (--psm, tessdata, whitelist)."""
    options = ["--psm %d" % int(reglages_ocr.get("psm", 6))]
    option_tessdata = _option_tessdata(reglages_ocr.get("tessdata_dir", ""))
    if option_tessdata:
        options.append(option_tessdata)
    whitelist = reglages_ocr.get("whitelist", "")
    if whitelist:
        dossier = dossier_travail or DOSSIER_DONNEES
        # Le fichier de config doit rester le dernier argument.
        options.append(ecrire_fichier_whitelist(whitelist, dossier))
    return " ".join(options)


def _pretraiter(image, upscale: int):
    """Niveaux de gris, agrandissement, et inversion si fond sombre."""
    from PIL import Image, ImageOps

    gris = image.convert("L")
    if upscale and upscale > 1:
        gris = gris.resize(
            (gris.width * upscale, gris.height * upscale), Image.LANCZOS
        )
    # Le fil de commandes du fournisseur est du texte clair sur fond sombre : Tesseract prefere l'inverse.
    histogramme = gris.histogram()
    total = sum(histogramme) or 1
    moyenne = sum(i * n for i, n in enumerate(histogramme)) / total
    if moyenne < 128:
        gris = ImageOps.invert(gris)
    return ImageOps.autocontrast(gris)


def fenetre_active_est_le_jeu() -> Optional[bool]:
    """True si la fenetre au premier plan est le logiciel du fournisseur (ou ExziHub), False sinon.

    None quand on ne sait pas (hors Windows, erreur API) : l'appelant ne doit
    alors pas suspendre la capture.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            tampon = ctypes.create_unicode_buffer(1024)
            taille = wintypes.DWORD(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, tampon, ctypes.byref(taille)):
                return None
        finally:
            kernel32.CloseHandle(handle)
        exe = os.path.basename(tampon.value).lower()
        return "fournisseur" in exe or "exzihub" in exe
    except Exception:  # pragma: no cover - API Windows indisponible
        return None


class PipelineOCR:
    """Orchestration des threads de capture (un ou deux) et d'OCR.

    Zone rapide + zone de securite (optionnel, si capture.zone_rapide est
    configuree) : une petite zone (les dernieres lignes du chat) est capturee
    et lue tres souvent pour un retour immediat, pendant que la zone habituelle
    (plus grande) est relue moins souvent en filet de securite, pour rattraper
    une rafale d'achats trop rapide pour la petite zone seule. Les deux
    alimentent le meme flux de texte (sur_texte) : la deduplication (voir
    parsing.DedupeurDeFlux) absorbe le recouvrement entre les deux.

    Sans zone_rapide (comportement d'origine) : une seule zone, une seule
    cadence, comme avant.
    """

    def __init__(self, config: dict, sur_texte: Callable[[str, str], None]) -> None:
        self.config = config
        self.sur_texte = sur_texte
        cap = config["capture"]

        zone_rapide = cap.get("zone_rapide")
        if zone_rapide:
            self.boite = verifier_zone(cap, "zone_rapide")
            self.boite_securite = verifier_zone(cap, "zone")
        else:
            self.boite = verifier_zone(cap, "zone")
            self.boite_securite = None
        self.intervalle = float(cap.get("interval_s", 0.25))
        self.intervalle_securite = float(cap.get("interval_securite_s", 2.0))
        self.monitor = cap.get("monitor", 1)

        self.file: "queue.Queue[Capture]" = queue.Queue(maxsize=cap.get("queue_maxsize", 300))
        # Petite : la zone de securite n'a besoin que de la derniere image.
        self.file_securite: "queue.Queue[Capture]" = queue.Queue(maxsize=1)

        ocr = config["ocr"]
        self.lang = ocr.get("lang", "fra")
        self.upscale = int(ocr.get("upscale", 2))
        self.config_tesseract = construire_config_tesseract(ocr)

        self._arret = threading.Event()
        self._threads: list = []
        self.captures_perdues = 0
        self.captures_identiques = 0
        self.erreurs_ocr = 0
        self.duree_ocr_ms = 0.0
        self.duree_ocr_ms_securite = 0.0
        self.pause_hors_jeu = bool(cap.get("pause_hors_jeu", True))
        self.hors_jeu = False
        self._dernier_test_jeu = 0.0
        enregistrement = cap.get("enregistrer_textes") or None
        self.fichier_enregistrement = chemin_donnees(enregistrement) if enregistrement else None
        self.derniere_erreur: Optional[str] = None

    # ------------------------------------------------------------------ etat
    @property
    def taille_queue(self) -> int:
        return self.file.qsize() + self.file_securite.qsize()

    @property
    def actif(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    # ------------------------------------------------------------- cycle vie
    def demarrer(self) -> None:
        if self.actif:
            return
        self._arret.clear()
        self._threads = [
            threading.Thread(target=self._boucle_capture,
                             args=(self.boite, self.intervalle, self.file, "principale"),
                             name="capture", daemon=True),
            threading.Thread(target=self._boucle_consommateur,
                             args=(self.file, "principale"), name="ocr", daemon=True),
        ]
        if self.boite_securite:
            # Threads separes (capture ET ocr) de la zone principale : sinon
            # l'OCR couteux de la zone de securite (large image) retarderait
            # le traitement de la zone rapide pendant qu'il tourne. pytesseract
            # lance un sous-processus tesseract.exe : le GIL est relache
            # pendant l'attente, les deux OCR tournent reellement en parallele.
            self._threads.append(threading.Thread(
                target=self._boucle_capture,
                args=(self.boite_securite, self.intervalle_securite, self.file_securite, "securite"),
                name="capture-securite", daemon=True))
            self._threads.append(threading.Thread(
                target=self._boucle_consommateur,
                args=(self.file_securite, "securite"), name="ocr-securite", daemon=True))
        for thread in self._threads:
            thread.start()
        if self.boite_securite:
            LOG.info("Pipeline OCR demarre : zone rapide %s (%.1fs) + zone de securite %s (%.1fs)",
                     self.boite, self.intervalle, self.boite_securite, self.intervalle_securite)
        else:
            LOG.info("Pipeline OCR demarre sur la zone %s", self.boite)

    def arreter(self, timeout: float = 3.0) -> None:
        self._arret.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        self.vider()
        LOG.info("Pipeline OCR arrete")

    def vider(self) -> int:
        """Jette les captures en attente (les deux zones) ; retourne leur nombre."""
        jetees = 0
        for file_cible in (self.file, self.file_securite):
            while True:
                try:
                    file_cible.get_nowait()
                    jetees += 1
                except queue.Empty:
                    break
        return jetees

    # ------------------------------------------------------------- producteur
    def _suspendu_hors_jeu(self) -> bool:
        """Vrai si le logiciel du fournisseur n'est pas au premier plan (test au plus 2 fois/s)."""
        if not self.pause_hors_jeu:
            self.hors_jeu = False
            return False
        maintenant = time.monotonic()
        if maintenant - self._dernier_test_jeu >= 0.5:
            self._dernier_test_jeu = maintenant
            self.hors_jeu = fenetre_active_est_le_jeu() is False
        return self.hors_jeu

    def _enregistrer_texte(self, texte: str, nom: str) -> None:
        """Ajoute le texte OCR brut a un fichier JSONL (rejeu hors ligne), plafonne a 5 Mo."""
        chemin = self.fichier_enregistrement
        if not chemin:
            return
        try:
            import json
            if os.path.exists(chemin) and os.path.getsize(chemin) > 5_000_000:
                return
            with open(chemin, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "source": nom, "texte": texte},
                                   ensure_ascii=False) + chr(10))
        except OSError:
            LOG.exception("Enregistrement des textes OCR impossible")

    def _empiler(self, file_cible: "queue.Queue[Capture]", capture: Capture) -> None:
        """Empile en jetant la capture la PLUS ANCIENNE si la file est pleine.

        L'OCR est plus lent que la capture : mieux vaut perdre une image
        perimee que prendre du retard sur l'etat reel du chat.
        """
        while True:
            try:
                file_cible.put_nowait(capture)
                return
            except queue.Full:
                try:
                    file_cible.get_nowait()
                    file_cible.task_done()
                    self.captures_perdues += 1
                except queue.Empty:  # pragma: no cover - course benigne
                    pass

    def _boucle_capture(self, boite: dict, intervalle: float,
                        file_cible: "queue.Queue[Capture]", nom: str) -> None:
        """Boucle de capture generique : une zone, une cadence, une file.

        Lancee une fois pour la zone principale, et une seconde fois (thread
        separe, cadence separee) pour la zone de securite si configuree.
        """
        import hashlib

        import mss
        from PIL import Image

        empreinte_precedente = None
        try:
            with _capteur_mss(mss)() as capteur:
                while not self._arret.is_set():
                    debut = time.monotonic()
                    if self._suspendu_hors_jeu():
                        empreinte_precedente = None
                        self._arret.wait(0.5)
                        continue
                    try:
                        brut = capteur.grab(boite)
                        # L'OCR coute cher : inutile de relire une image
                        # inchangee, elle donnerait exactement le meme texte.
                        empreinte = hashlib.blake2b(
                            bytes(brut.bgra), digest_size=16).digest()
                        if empreinte == empreinte_precedente:
                            self.captures_identiques += 1
                        else:
                            empreinte_precedente = empreinte
                            image = Image.frombytes(
                                "RGB", brut.size, brut.bgra, "raw", "BGRX")
                            self._empiler(file_cible, Capture(time.time(), image))
                    except Exception as exc:
                        self.derniere_erreur = "Capture (%s) : %s" % (nom, exc)
                        LOG.exception("Erreur de capture d'ecran (%s)", nom)
                        time.sleep(1.0)
                    reste = intervalle - (time.monotonic() - debut)
                    if reste > 0:
                        self._arret.wait(reste)
        except Exception as exc:  # pragma: no cover - defaillance mss globale
            self.derniere_erreur = "Producteur (%s) arrete : %s" % (nom, exc)
            LOG.exception("Thread producteur interrompu (%s)", nom)

    # ------------------------------------------------------------ consommateur
    def _boucle_consommateur(self, file_source: "queue.Queue[Capture]", nom: str) -> None:
        """Un thread OCR par zone : celui de la zone de securite (image large,
        donc lent) ne doit jamais retarder celui de la zone rapide."""
        import pytesseract

        chemin = self.config.get("tesseract_cmd")
        if chemin:
            pytesseract.pytesseract.tesseract_cmd = chemin

        while not self._arret.is_set():
            try:
                capture = file_source.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                debut = time.perf_counter()
                image = _pretraiter(capture.image, self.upscale)
                texte = pytesseract.image_to_string(
                    image, lang=self.lang, config=self.config_tesseract
                )
                duree = (time.perf_counter() - debut) * 1000
                if nom == "securite":
                    self.duree_ocr_ms_securite = duree
                else:
                    self.duree_ocr_ms = duree
                LOG.debug("[%s] Queue=%d | %.0f ms | OCR : %r",
                          nom, self.taille_queue, duree, texte)
                self._enregistrer_texte(texte, nom)
                self.sur_texte(texte, nom)
            except Exception as exc:
                self.erreurs_ocr += 1
                self.derniere_erreur = "OCR (%s) : %s" % (nom, exc)
                LOG.exception("Erreur OCR (%s, total %d)", nom, self.erreurs_ocr)
                time.sleep(0.5)
            finally:
                file_source.task_done()
