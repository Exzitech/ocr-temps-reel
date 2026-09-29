"""Faux fil de commandes du fournisseur, pour essayer l'outil sans lancer le logiciel du fournisseur.

Affiche une fenetre qui imite l'onglet systeme du chat et debite des messages
d'achat realistes. On pointe la zone de capture dessus (bouton "Zone de
capture" de la fenetre principale) et on regarde les compteurs monter.

    python demo_chat.py                  # ingredients du Salade de fruits
    python demo_chat.py "Blanquette de veau"

Bouton "Rafale" : 3 achats en une seconde, pour voir le comportement en
conditions difficiles.
"""
from __future__ import annotations

import random
import sys
import tkinter as tk
from datetime import datetime
from typing import List, Tuple

NB_LIGNES = 10
COULEUR_FOND = "#141418"
COULEUR_TEXTE = "#d8d8cf"

# Repli si l'API du catalogue n'est pas joignable.
INGREDIENTS_SECOURS: List[Tuple[str, int]] = [
    ("Gelée Myrtille", 120),
    ("Gelée à la Fraise", 140),
    ("Gelée à la Menthe", 95),
    ("Gelée Citron", 210),
    ("Épices de Cayenne", 6000),
]


def _milliers(nombre: int) -> str:
    """Comme le logiciel du fournisseur : separateur de milliers par une espace ("1 067")."""
    return "{:,}".format(nombre).replace(",", " ")


def ingredients_de(nom_item: str) -> List[Tuple[str, int]]:
    """(nom, prix unitaire indicatif) des ingredients d'une recette."""
    try:
        from catalogue import ClientCatalogue

        recette = ClientCatalogue().recette(nom_item)
        print("Recette %s : %d ingredients" % (recette.nom, len(recette.ingredients)))
        return [(ing.nom, random.randint(80, 4000)) for ing in recette.ingredients]
    except Exception as exc:
        print("catalogue de recettes indisponible (%s), ingredients de secours." % exc)
        return INGREDIENTS_SECOURS


class FauxChat(tk.Tk):
    def __init__(self, ingredients: List[Tuple[str, int]]) -> None:
        super().__init__()
        self.ingredients = ingredients
        self.lignes: List[str] = []

        self.title("Faux fil de commandes du fournisseur (demo)")
        self.geometry("900x300+40+700")
        self.configure(bg=COULEUR_FOND)
        self.attributes("-topmost", True)

        self.zone_texte = tk.Text(
            self, bg=COULEUR_FOND, fg=COULEUR_TEXTE, font=("Segoe UI", 11),
            bd=0, highlightthickness=0, wrap="none", height=NB_LIGNES,
        )
        self.zone_texte.pack(fill="both", expand=True, padx=6, pady=6)
        self.zone_texte.configure(state="disabled")

        barre = tk.Frame(self, bg=COULEUR_FOND)
        barre.pack(fill="x", padx=6, pady=(0, 6))
        tk.Button(barre, text="Achat", command=self.achat).pack(side="left")
        tk.Button(barre, text="Rafale (3/s)", command=self.rafale).pack(side="left", padx=4)
        tk.Button(barre, text="Vente (ignoree)",
                  command=self.vente).pack(side="left")
        self.var_auto = tk.BooleanVar(value=True)
        tk.Checkbutton(barre, text="auto", variable=self.var_auto, bg=COULEUR_FOND,
                       fg=COULEUR_TEXTE, selectcolor=COULEUR_FOND,
                       activebackground=COULEUR_FOND).pack(side="left", padx=6)
        self.var_total = tk.StringVar(value="0 achat emis")
        tk.Label(barre, textvariable=self.var_total, bg=COULEUR_FOND,
                 fg="#888888").pack(side="right")

        self.nb_achats = 0
        self.after(1500, self._boucle)

    # ------------------------------------------------------------- messages
    def _ajouter(self, ligne: str) -> None:
        self.lignes.append(ligne)
        self.lignes = self.lignes[-NB_LIGNES:]
        self.zone_texte.configure(state="normal")
        self.zone_texte.delete("1.0", "end")
        self.zone_texte.insert("1.0", "\n".join(self.lignes))
        self.zone_texte.configure(state="disabled")

    def achat(self) -> None:
        nom, prix_unitaire = random.choice(self.ingredients)
        quantite = random.choice([1, 1, 2, 5, 10, 10, 20])
        prix = quantite * prix_unitaire
        heure = datetime.now().strftime("%H:%M")
        self._ajouter("[%s] %d x [%s] (%s euros)"
                      % (heure, quantite, nom, _milliers(prix)))
        self.nb_achats += 1
        self.var_total.set("%d achat(s) emis" % self.nb_achats)

    def vente(self) -> None:
        """Message qui ne doit PAS etre compte."""
        heure = datetime.now().strftime("%H:%M")
        self._ajouter("[%s] Banque : + %s Euros (vente)"
                      % (heure, _milliers(random.randint(1000, 50000))))

    def rafale(self) -> None:
        """3 achats en une seconde : le cas ou l'OCR peut rater une ligne."""
        for delai in (0, 330, 660):
            self.after(delai, self.achat)

    def _boucle(self) -> None:
        if self.var_auto.get():
            self.achat()
        self.after(random.randint(1800, 3500), self._boucle)


def main() -> int:
    nom_item = sys.argv[1] if len(sys.argv) > 1 else "Salade de fruits"
    FauxChat(ingredients_de(nom_item)).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
