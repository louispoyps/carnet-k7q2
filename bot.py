"""
Bot achat-revente de consoles Leboncoin (et Vinted) -> Telegram.

Pour chaque console de ta grille (config.yaml), le bot :
  - cherche les nouvelles annonces du jour, avec livraison, à prix bas,
  - écarte les consoles en panne, "main propre uniquement", accessoires seuls...,
  - calcule le prix du marché en direct et le compare à ta fourchette de revente,
  - calcule ton bénéfice estimé et t'envoie les bonnes affaires sur Telegram.

Usage :
    python bot.py               boucle infinie
    python bot.py --once        un seul passage
    python bot.py --dry-run     affiche dans la console au lieu d'envoyer
    python bot.py --marche      état du marché de chaque console vs ta grille
    python bot.py --chat-id     affiche ton chat_id Telegram
    python bot.py --test        envoie un message de test sur Telegram
"""

import argparse
import html
import json
import logging
import logging.handlers
import os
import random
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import lbc
import yaml
from curl_cffi import requests
from lbc.exceptions import DatadomeError

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
SECRET_FILE = BASE_DIR / "secret.yaml"  # token Telegram local, jamais envoyé sur GitHub
SEEN_FILE = BASE_DIR / "annonces_vues.json"

PARIS = ZoneInfo("Europe/Paris")
CATEGORY = lbc.Category.ELECTRONIQUE_CONSOLES
OTHER_CATEGORY = lbc.Category.ELECTRONIQUE_JEUX_VIDEO

sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Journal limité à 3 fichiers de 2 Mo, pour ne pas remplir le disque du serveur
        logging.handlers.RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=2_000_000,
                                             backupCount=2, encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")


# ------------------------------------------------------------------ outils

def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if SECRET_FILE.exists():
        with open(SECRET_FILE, encoding="utf-8") as f:
            cfg["telegram"].update(yaml.safe_load(f).get("telegram", {}))
    cfg["produits"] = [p for p in cfg["produits"] if p.get("actif", True)]
    return cfg


def load_seen() -> dict | None:
    """Annonces déjà vues, ou None si le fichier n'existe pas ou est abîmé."""
    if not SEEN_FILE.exists():
        return None
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log.warning("Mémoire des annonces illisible (%s) : on repart de zéro", e)
        return None


def save_seen(seen: dict) -> None:
    limit = (datetime.now() - timedelta(days=60)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= limit}
    # Écrit dans un fichier temporaire puis le renomme : une coupure pendant
    # l'écriture ne peut pas abîmer la mémoire
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen), encoding="utf-8")
    os.replace(tmp, SEEN_FILE)


def attr(ad: lbc.Ad, key: str) -> str | None:
    a = ad.attributes.get(key)
    return a.value if a else None


def attr_label(ad: lbc.Ad, key: str) -> str | None:
    a = ad.attributes.get(key)
    return a.value_label if a else None


def to_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def eur(value: float) -> str:
    return f"{value:,.0f} €".replace(",", " ")


def plain(text: str) -> str:
    """Texte sans accents ("série" -> "serie")."""
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


def has_word(text: str, word: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text) is not None


# "2 jeux", "avec manette" : lot avec la console ; "sans manette" : la console vendue seule
BUNDLE_BEFORE = re.compile(r"(?<!\w)(\d+|un|une|deux|trois|quatre|cinq|de|des|plus|avec|sans)\s*$")
NEGATION_BEFORE = re.compile(r"(?<!\w)(pas|aucun|aucune|sans|jamais|ni|non|zéro|zero)(?!\w)")
NEGATION_AFTER = re.compile(r"^\s*(pas|jamais|plus)(?!\w)")


def mentions(text: str, word: str) -> bool:
    """Le mot apparaît en entier et sans être nié : "surchauffe" oui, "ne surchauffe pas" /
    "aucun problème de surchauffe" non ; "complète" oui, "incomplète" / "pas complète" non.
    Les expressions déjà négatives ("ne marche pas", "sans dock") sont cherchées telles quelles."""
    if word.startswith("ne ") or " pas" in word or word.startswith("sans "):
        return has_word(text, word)
    for m in re.finditer(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text):
        before = re.split(r"[.!?\n;]", text[max(0, m.start() - 40):m.start()])[-1]
        after = text[m.end():m.end() + 12]
        if NEGATION_BEFORE.search(before) or NEGATION_AFTER.search(after):
            continue
        return True
    return False


def round_down(value: float, step: int = 5) -> int:
    return int(value // step * step)


def title_of(ad: lbc.Ad) -> str:
    return html.unescape(ad.subject or "")


def make_client() -> lbc.Client:
    """Client Leboncoin, via un proxy si PROXY_URL est défini (ex : http://user:mdp@hote:port)."""
    url = os.environ.get("PROXY_URL")
    if not url:
        return lbc.Client()
    u = urlsplit(url)
    port = u.port
    # DataImpulse, port 823 : nouvelle IP à chaque connexion. Le cookie DataDome est lié à l'IP :
    # dès que le proxy rouvre la connexion (entre deux passages), Leboncoin bloque. Les ports
    # 10000-20000 gardent la même IP 30 min : un port tiré au hasard par connexion.
    if u.hostname == "gw.dataimpulse.com" and port == 823:
        port = random.randint(10000, 19999)
    # Identifiants laissés encodés (%40...) : curl les décode lui-même
    client = lbc.Client(proxy=lbc.Proxy(host=u.hostname, port=port, username=u.username,
                                        password=u.password, scheme=u.scheme))
    # Après une pause, la 1re connexion via l'IP fixe échoue souvent au bout de 15-25 s
    # (le réessai passe aussitôt) : on abandonne plus tôt. Une recherche normale prend < 3 s.
    client.timeout = 15
    return client


def posted_at(ad: lbc.Ad) -> str:
    """Dernière mise en ligne de l'annonce (publication ou remontée en tête)."""
    return max(ad.first_publication_date or "", getattr(ad, "index_date", None) or "")


def search(client: lbc.Client, product: dict, cfg: dict, limit: int,
           extra: bool = False, **kwargs) -> list[lbc.Ad]:
    """Annonces de consoles (pas d'accessoires) livrables pour ce produit.

    extra=True : cherche aussi par mot-clé dans « Jeux vidéo », où des vendeurs
    rangent leur console par erreur."""
    # Pas de filtre « type = console » : des vendeurs ne le remplissent pas.
    # Les manettes et accessoires sont écartés ensuite par rejection().
    ads = client.search(
        category=CATEGORY, limit=limit, console_model=product["modeles"],
        shipping_type=cfg["modes_envoi"], **kwargs,
    ).ads
    if extra and product.get("mot_cle"):
        try:
            found = client.search(
                text=product["mot_cle"], search_in_title_only=True, category=OTHER_CATEGORY,
                limit=limit, shipping_type=cfg["modes_envoi"], **kwargs,
            ).ads
        except Exception as e:
            log.warning("  recherche par mot-clé impossible (%s)", e)
            found = []
        ids = {ad.id for ad in ads}
        ads += [ad for ad in found if ad.id not in ids]
    return ads


def misfiled(ad: lbc.Ad) -> bool:
    """Annonce rangée hors de la catégorie Consoles (trouvée par mot-clé)."""
    category = getattr(ad, "category_id", None)
    return category is not None and str(category) != CATEGORY.value


def rejection(ad: lbc.Ad, product: dict, cfg: dict) -> str | None:
    """Motif de rejet d'une annonce (hors prix), ou None si elle est acceptable."""
    title = title_of(ad).lower()
    text = f"{title} {ad.body or ''}".lower()
    for w in cfg.get("titre_exclut", []) + product.get("titre_exclut", []):
        if has_word(title, w):
            return f"titre « {w} »"
    # Vinted ne filtre ni la catégorie ni le modèle : le titre doit nommer la console
    wanted = product.get("vinted_titre")
    if getattr(ad, "site", None) == "Vinted" and wanted \
            and not any(has_word(plain(title), plain(w)) for w in wanted):
        return "pas ce modèle"
    # Catégorie et marque lues sur la page Vinted (seulement pour les annonces rentables) :
    # un « Réveil manette de ps5 » est rangé dans « Horloges », une fausse manette a une autre marque
    catalog = getattr(ad, "vinted_catalog", None)
    if catalog and product.get("vinted_categories") and catalog not in product["vinted_categories"]:
        return f"catégorie Vinted « {getattr(ad, 'vinted_catalog_name', catalog)} »"
    brand = getattr(ad, "vinted_brand", None)
    if brand and product.get("vinted_marques") and not any(m in brand for m in product["vinted_marques"]):
        return f"marque « {brand} »"
    if re.search(r"pour\s+pi[eè]ces?(?!\w)", title):
        return "vendue pour pièces"
    if product.get("accessoire"):
        return accessory_rejection(ad, title, text, product, cfg)
    first = re.sub(r"^[^\w]+", "", title).split(" ", 1)[0]
    if first in cfg.get("titre_debut_exclut", []):
        return f"accessoire (titre commence par « {first} »)"
    if re.match(r"\W*lots?\s+(de\s+)?(\d+\s+)?(jeux|jeu|manettes?|accessoires|cartes?)(?!\w)", title):
        return "lot de jeux ou d'accessoires"
    head = re.split(r"\+|\bavec\b|&|\bet\b", title, maxsplit=1)[0]
    for w in cfg.get("mots_accessoires", []):
        for m in re.finditer(r"(?<!\w)" + re.escape(w) + r"(?!\w)", head):
            # "2 jeux", "3 manettes", "1to de jeux" : c'est un lot avec la console
            if not BUNDLE_BEFORE.search(head[:m.start()]):
                return f"accessoire seul (« {w} »)"
    if misfiled(ad):
        # Rangée dans « Jeux vidéo » (ou trouvée sur Vinted, sans catégorie) : beaucoup de jeux
        # et de collectors. Le début du titre (avant « + » / « avec » / « sans ») ne doit
        # parler que de la console.
        head = re.split(r"\bsans\b", head, maxsplit=1)[0]
        # Le modèle saisi par le vendeur y est parfois faux : le titre fait foi
        model = attr(ad, "console_model")
        if model and model not in product["modeles"] and not has_word(plain(title), plain(product["mot_cle"])):
            return f"autre modèle ({model})"
        allowed = set(cfg.get("mots_titre_console", []))
        others = [w for w in re.findall(r"[\w']+", head) if w not in allowed and not w.isdigit()]
        if others:
            return f"hors catégorie, pas une console seule (« {' '.join(others[:3])} »)"
    weight = to_int(attr(ad, "estimated_parcel_weight"))
    if 200 <= weight < product.get("poids_min_g", 0):  # < 200 g : poids non renseigné
        return f"colis trop léger pour la console ({weight} g)"
    if attr(ad, "video_game_type") not in (None, "console"):
        return "pas une console"
    return common_rejection(ad, text, product, cfg)


def common_rejection(ad: lbc.Ad, text: str, product: dict, cfg: dict) -> str | None:
    """Règles communes aux consoles et aux accessoires : état, prix, livraison."""
    if ad.price and ad.price < product.get("prix_min", 0):
        return f"prix trop bas ({eur(ad.price)})"
    if attr(ad, "condition") == "pourpieces":
        return "vendue pour pièces"
    if attr(ad, "shippable") != "true":
        return "pas de livraison"
    for w in cfg.get("mots_main_propre", []):
        if w in text:
            return f"main propre uniquement (« {w} »)"
    return None


def accessory_rejection(ad: lbc.Ad, title: str, text: str, product: dict, cfg: dict) -> str | None:
    """Accessoire vendu seul (manette) : le titre doit le nommer, et ce n'est pas une console."""
    if not any(has_word(title, w) for w in product.get("titre_requis", [])):
        return "pas une manette"
    model = attr(ad, "console_model")
    if model and model not in product["modeles"]:
        return f"autre modèle ({model})"
    if attr(ad, "video_game_type") not in (None, "accessoires"):
        return "pas un accessoire seul"
    return common_rejection(ad, text, product, cfg)


def problem_words(ad: lbc.Ad, cfg: dict) -> list[str]:
    """Mots inquiétants présents dans l'annonce (sans être niés) : signalés dans l'alerte."""
    text = f"{title_of(ad)} {ad.body or ''}".lower()
    return [w for w in cfg.get("mots_a_verifier", []) if mentions(text, w)]


SCAM_CONTACT = re.compile(r"(?:\+33|0033|(?<!\d)0)\s*[67](?:[\s.\-]*\d{2}){4}(?!\d)"  # 06 12 34 56 78
                          r"|[\w.\-]+@[\w\-]+\.[a-z]{2,}"                            # adresse mail
                          r"|wa\.me/")


def scam_words(ad: lbc.Ad, cfg: dict) -> list[str]:
    """Signes d'arnaque (paiement ou contact hors du site) : virement, WhatsApp, numéro..."""
    text = f"{title_of(ad)} {ad.body or ''}".lower()
    found = [w for w in cfg.get("mots_arnaque", []) if mentions(text, w)]
    if SCAM_CONTACT.search(text):
        found.append("numéro ou mail pour le contacter")
    return found


def fingerprint(ad: lbc.Ad) -> str:
    """Empreinte de l'annonce, pour reconnaître une annonce republiée sous un autre numéro."""
    title = re.sub(r"\W+", " ", title_of(ad).lower()).strip()
    if getattr(ad, "site", None) == "Vinted":
        # Pas de code postal sur Vinted : le vendeur fait la différence entre deux « PS5 »
        return f"fp:vinted:{ad.seller}|{title}"
    return f"fp:{title}|{ad.location.zipcode}|{attr(ad, 'console_model')}"


def cross_key(ad: lbc.Ad) -> str:
    """Même titre et même prix : la même console mise en vente sur Leboncoin et sur Vinted."""
    title = re.sub(r"\W+", " ", title_of(ad).lower()).strip()
    return f"fp:titre_prix:{title}|{round(float(ad.price or 0))}"


def already_seen(ad: lbc.Ad, seen: dict) -> bool:
    if str(ad.id) in seen or fingerprint(ad) in seen:
        return True
    # Seulement sur 2 jours : deux « Nintendo switch » au même prix à un mois d'écart sont deux consoles
    return seen.get(cross_key(ad), "") >= (datetime.now() - timedelta(days=2)).isoformat()


def mark_seen(ad: lbc.Ad, seen: dict) -> None:
    now = datetime.now().isoformat()
    seen[str(ad.id)] = now
    seen[fingerprint(ad)] = now
    seen[cross_key(ad)] = now


# ------------------------------------------------------------- prix marché

class MarketPrice:
    """Prix du marché de chaque console (annonces livrables en état de marche), avec cache."""

    def __init__(self, client: lbc.Client, cache_minutes: int = 120):
        self.client = client
        self.ttl = cache_minutes * 60
        self.cache: dict[str, tuple[float, list[float]]] = {}

    def prices(self, product: dict, cfg: dict) -> list[float]:
        name = product["nom"]
        if name not in self.cache or time.time() - self.cache[name][0] > self.ttl:
            ads = []
            for page in range(1, cfg["rentabilite"].get("pages_marche", 3) + 1):
                extra = {"price": product["prix_marche"]} if product.get("prix_marche") else {}
                batch = search(self.client, product, cfg, limit=100, page=page, **extra)
                ads += batch
                if len(batch) < 100:
                    break
                time.sleep(1)
            # Le prix du marché ne compte que les consoles sans mot inquiétant
            prices = sorted(a.price for a in ads if a.price and not rejection(a, product, cfg)
                            and not problem_words(a, cfg))
            self.cache[name] = (time.time(), prices)
            time.sleep(1.5)
        return self.cache[name][1]

    def stats(self, product: dict, cfg: dict) -> tuple[float | None, float | None, int]:
        """(médiane, prix de vente rapide, nombre d'annonces).

        Prix de vente rapide = prix des 25 % les moins chères : pour revendre vite,
        il faut se placer parmi les moins chers, pas au prix médian affiché.
        """
        prices = self.prices(product, cfg)
        if len(prices) < 6:
            return None, None, len(prices)
        return statistics.median(prices), prices[len(prices) // 4], len(prices)


# ----------------------------------------------------------------- analyse

def rent_for(product: dict, cfg: dict) -> dict:
    """Réglages de rentabilité, avec ceux propres au produit (manettes : port et marge plus petits)."""
    return {**cfg["rentabilite"], **(product.get("rentabilite") or {})}


def resale_for(live: float, quick: float, product: dict, rent: dict) -> float:
    """Prix de revente estimé : vente rapide (ou médiane si demandé), plafonné par ta grille."""
    base = live if rent.get("revente_marche") == "mediane" else quick
    low, high = product["revente"]
    return min(base, (low + high) / 2) if rent.get("prudent", True) else base


def costs_for(price: float, rent: dict) -> float:
    return rent["frais_achat_fixe"] + price * rent["frais_achat_pct"] / 100


def margin_for(resale: float, rent: dict) -> float:
    """Bénéfice minimum : un montant fixe, ou un % du prix de revente si c'est plus."""
    return max(rent["marge_min"], resale * rent.get("marge_min_pct", 0) / 100)


def max_buy_for(net_resale: float, margin: float, rent: dict, repair_cost: float = 0) -> float:
    """Prix d'achat le plus haut qui laisse encore la marge minimum."""
    return (net_resale - rent["frais_achat_fixe"] - repair_cost - margin) \
        / (1 + rent["frais_achat_pct"] / 100)


def analyse(ad: lbc.Ad, product: dict, market: MarketPrice, cfg: dict) -> dict | None:
    """Calcule la rentabilité ; renvoie l'analyse si l'affaire vaut le coup, sinon None."""
    rent = rent_for(product, cfg)
    price = ad.price
    if not price:
        return None
    reason = rejection(ad, product, cfg)
    if reason:
        log.info("  ignorée (%s) : %s", reason, title_of(ad))
        return None
    # Paiement ou contact hors du site (virement, PayPal, WhatsApp...) : jamais envoyée
    scams = scam_words(ad, cfg)
    if scams:
        log.info("  ignorée (arnaque probable : %s) : %s", ", ".join(scams), title_of(ad))
        return None

    text = f"{title_of(ad)} {ad.body or ''}".lower()
    good, warn = [], []
    repairs = {w: c for w, c in (cfg.get("frais_reparation") or {}).items() if mentions(text, w)}
    repair_cost = sum(repairs.values())
    if repairs:
        warn.append("À prévoir : " + ", ".join(repairs) + f" (+{eur(repair_cost)})")
    good += [f"« {w} »" for w in cfg.get("mots_positifs", []) if mentions(text, w)]
    problems = problem_words(ad, cfg)
    if problems:
        warn.insert(0, "🚨 L'annonce mentionne : " + ", ".join(f"« {w} »" for w in problems)
                    + " — lis la description et demande au vendeur si ça marche")
    if attr(ad, "condition") == "etatsatisfaisant":
        warn.append("État satisfaisant (traces d'usure)")

    # --- prix du marché : en direct, comparé à ta fourchette de revente
    low, high = product["revente"]
    mid = (low + high) / 2
    live, quick, n = market.stats(product, cfg)
    if live:
        if quick < low * 0.9:
            market_note = f"⬇️ plus bas que ta revente prévue ({eur(low)}–{eur(high)})"
        elif quick > high * 1.1:
            market_note = f"⬆️ plus haut que ta revente prévue ({eur(low)}–{eur(high)})"
        else:
            market_note = f"✔ conforme à ta revente prévue ({eur(low)}–{eur(high)})"
        resale = resale_for(live, quick, product, rent)
    else:
        market_note = f"pas assez d'annonces ({n}), estimation via ta grille"
        resale = mid

    # --- rentabilité
    net_resale = resale * (1 - rent["negociation_revente_pct"] / 100)
    costs = costs_for(price, rent) + repair_cost
    profit = net_resale - price - costs
    margin = margin_for(resale, rent)
    max_buy = min(max_buy_for(net_resale, margin, rent, repair_cost), product["achat"][1])
    if price > max_buy * (1 + rent["marge_negociation_achat_pct"] / 100):
        log.info("  trop cher %s (max %s) : %s", eur(price), eur(max_buy), title_of(ad))
        return None

    site = getattr(ad, "site", "Leboncoin")
    if price < resale * rent.get("prix_suspect_pct", 55) / 100:
        warn.insert(0, "Prix anormalement bas : arnaque possible. Paie UNIQUEMENT via le "
                       f"paiement sécurisé {site}, jamais par virement ou en dehors du site")

    score, count = attr(ad, "rating_score"), attr(ad, "rating_count")
    if to_int(count) > 0:
        stars = float(score or 0) * 5
        (good if stars >= 4.5 else warn).append(f"Vendeur noté {stars:.1f}/5 ({count} avis)")
    elif count is None and site == "Vinted":
        warn.append("Avis du vendeur non lus (page Vinted injoignable)")
    else:
        warn.append("Vendeur sans avis")

    buy_low = product["achat"][0]
    if price <= max_buy and price <= buy_low:
        label = "🔥🔥 AFFAIRE EN OR"
        offer = price
    elif price <= max_buy:
        label = "🔥 BONNE AFFAIRE"
        offer = max(buy_low, round_down(price * 0.93))
    else:
        label = "💬 À NÉGOCIER"
        offer = round_down(max_buy)
    offer = min(offer, price)
    if problems:
        label = "🚨 À VÉRIFIER · " + label

    return {
        "label": label,
        "live": live,
        "quick": quick,
        "count": n,
        "market_note": market_note,
        "resale": resale,
        "net_resale": net_resale,
        "costs": costs,
        "profit": profit,
        "roi": profit / (price + costs) * 100,
        "max_buy": max_buy,
        "offer": offer,
        "offer_profit": net_resale - offer - costs_for(offer, rent) - repair_cost,
        "good": good,
        "warn": warn,
    }


# ---------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.dry_run = dry_run

    def _post(self, method: str, data: dict) -> bool:
        """Appel Telegram ; attend et réessaie si Telegram demande de ralentir (erreur 429)."""
        for attempt in range(3):
            try:
                r = requests.post(f"{self.api}/{method}", data=data, timeout=30)
            except Exception as e:
                log.warning("Telegram injoignable (%s), nouvel essai", e)
                time.sleep(5)
                continue
            if r.ok:
                return True
            if r.status_code == 429:
                wait = to_int(r.json().get("parameters", {}).get("retry_after")) or 5
                time.sleep(wait + 1)
                continue
            log.error("Erreur Telegram (%s) : %s", method, r.text)
            return False
        return False

    def send(self, text: str, photo: str | None = None, buttons: list | None = None) -> bool:
        """Envoie le message ; renvoie False s'il n'a pas pu partir.
        buttons : [(texte, lien), ...], affichés en boutons sous le message."""
        if self.dry_run:
            print("\n" + plain_text(text) + "\n" + "-" * 60)
            return True
        extra = {"chat_id": self.chat_id}
        if buttons:
            extra["reply_markup"] = json.dumps(
                {"inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]})
        if photo and len(plain_text(text)) <= 1024 and self._post("sendPhoto", dict(
                extra, photo=photo, caption=text, parse_mode="HTML")):
            return True
        # Sans photo (photo refusée ou texte trop long pour une légende)
        if self._post("sendMessage", dict(
                extra, text=text, parse_mode="HTML", disable_web_page_preview="true")):
            return True
        # Dernier essai en texte simple, si c'est la mise en forme que Telegram refuse
        return self._post("sendMessage", dict(
            extra, text=plain_text(text), disable_web_page_preview="true"))


def plain_text(text: str) -> str:
    """Texte tel que Telegram l'affiche (sans les balises HTML)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def buy_buttons(ad: lbc.Ad) -> list:
    site = getattr(ad, "site", "Leboncoin")
    return [(f"🛒 Acheter sur {site}", ad.url)]


def format_message(ad: lbc.Ad, a: dict, product: dict, cfg: dict) -> str:
    e = html.escape
    rent = rent_for(product, cfg)
    details = " · ".join(filter(None, [
        attr_label(ad, "console_model"),
        attr_label(ad, "condition"),
        attr_label(ad, "electronic_color"),
        "envoi " + (attr(ad, "shipping_type") or "").replace("_", " "),
    ]))
    if a["live"]:
        market = (f"📊 Marché : vente rapide {eur(a['quick'])}, médiane {eur(a['live'])} "
                  f"({a['count']} annonces) {a['market_note']}")
    else:
        market = f"📊 Marché : {a['market_note']}"
    site = getattr(ad, "site", None)
    lines = [
        f"<b>{a['label']} · {e(product['nom'])}</b>" + (f" · 🟢 <b>{site}</b>" if site else ""),
        f"🎮 {e(title_of(ad))}",
        f"📋 {e(details)}",
    ]
    if ad.location.city or ad.location.zipcode:
        lines.append(f"📍 {e(ad.location.city or '')} ({e(ad.location.zipcode or '')})")
    lines += [
        "",
        f"💶 Prix : <b>{eur(ad.price)}</b>  (ton achat {eur(product['achat'][0])}–"
        f"{eur(product['achat'][1])}, max {eur(a['max_buy'])})",
        e(market),
        f"🔁 Revente : {eur(a['resale'])} → {eur(a['net_resale'])} après "
        f"{rent['negociation_revente_pct']} % de négo",
        f"🧾 Frais d'achat (port, protection, réparations) : {eur(a['costs'])}",
        f"💰 <b>Bénéfice au prix affiché : {a['profit']:+,.0f} €</b> "
        f"(rentabilité {a['roi']:.0f} %)".replace(",", " "),
    ]
    if a["offer"] < ad.price:
        lines.append(f"🤝 Propose <b>{eur(a['offer'])}</b> → bénéfice "
                     f"{a['offer_profit']:+,.0f} €".replace(",", " "))
    if a["good"] or a["warn"]:
        lines.append("")
    lines += [f"✅ {e(g)}" for g in a["good"]]
    lines += [f"⚠️ {e(w)}" for w in a["warn"]]
    if a.get("auto_note"):
        lines += ["", a["auto_note"]]
    lines += ["", f'<a href="{ad.url}">Voir l\'annonce</a>']
    # Description du vendeur, pour juger soi-même. Raccourcie pour que le message tienne
    # sous la photo (1024 caractères), mais jamais à moins de 300 caractères.
    body = re.sub(r"\s*\n\s*", " / ", (ad.body or "").strip())
    if body:
        room = max(300, 1024 - len(plain_text("\n".join(lines))) - 20)
        if len(body) > room:
            body = body[:room - 1].rsplit(" ", 1)[0] + "…"
        lines[-2:-2] = ["", f"📝 <i>{e(body)}</i>"]
    return "\n".join(lines)


# ------------------------------------------------------------------- vinted

# État affiché par Vinted -> code Leboncoin (même traitement : « satisfaisant » signalé)
VINTED_CONDITIONS = {"neuf avec étiquette": "neuf", "neuf sans étiquette": "neuf",
                     "très bon état": "tresbonetat", "bon état": "bonetat",
                     "satisfaisant": "etatsatisfaisant"}


def vinted_ad(item: dict) -> SimpleNamespace:
    """Annonce Vinted présentée comme une annonce Leboncoin, pour réutiliser filtres et analyse."""
    condition = (item.get("item_box", {}).get("second_line") or "").split("·")[-1].strip()
    attrs = {"condition": (VINTED_CONDITIONS.get(condition.lower()), condition or None),
             "shippable": ("true", None), "shipping_type": ("Vinted", None)}
    photo = item.get("photo") or {}
    return SimpleNamespace(
        site="Vinted", id=f"v{item['id']}", seller=(item.get("user") or {}).get("id"),
        subject=item.get("title") or "", body="", price=float(item["price"]["amount"]),
        url="https://www.vinted.fr" + item["url"] if item["url"].startswith("/") else item["url"],
        images=[photo["url"]] if photo.get("url") else [], category_id="vinted",
        location=SimpleNamespace(city="", zipcode=""),
        attributes={k: SimpleNamespace(value=v, value_label=lab) for k, (v, lab) in attrs.items()},
    )


class Vinted:
    """Recherche Vinted via le proxy. Le jeton de session (valable 24 h) est gardé entre les passages."""

    HOME = "https://www.vinted.fr/how_it_works"
    API = "https://api.vinted.fr/svc-catalogue/items"

    def __init__(self):
        self.session = None
        self.since = 0.0

    def reset(self) -> None:
        self.session = None

    def _session(self) -> requests.Session:
        if self.session is None or time.time() - self.since > 20 * 3600:
            url = os.environ.get("PROXY_URL")
            s = requests.Session(impersonate="chrome", timeout=30,
                                 proxies={"http": url, "https": url} if url else None)
            s.get(self.HOME).raise_for_status()
            if not s.cookies.get("access_token_web"):
                raise RuntimeError("Vinted n'a pas donné de jeton de session")
            self.session, self.since = s, time.time()
        return self.session

    def search(self, text: str, price_min: int, price_max: int, per_page: int,
               page: int = 1) -> list[SimpleNamespace]:
        s = self._session()
        r = s.get(self.API, params={"search_text": text, "order": "newest_first",
                                    "price_from": price_min, "price_to": price_max,
                                    "per_page": per_page, "page": page},
                  headers={"Accept": "application/json", "Origin": "https://www.vinted.fr",
                           "Referer": "https://www.vinted.fr/",
                           "Authorization": f"Bearer {s.cookies.get('access_token_web')}"})
        if r.status_code in (401, 403):
            self.reset()  # jeton expiré ou refusé : nouvelle session au prochain essai
        r.raise_for_status()
        return [vinted_ad(item) for item in r.json().get("items", [])]

    def details(self, ad: SimpleNamespace) -> None:
        """Lit la page de l'annonce (lourde : seulement pour les alertes) : description et avis."""
        page = self._session().get(ad.url)
        page.raise_for_status()
        # Les données de la page sont du JSON rangé dans une chaîne : on retire un niveau d'échappement
        text = page.text.replace("\\\\", "\0").replace('\\"', '"').replace("\0", "\\")

        def field(key: str) -> str | None:
            m = re.search(r'"%s":("(?:[^"\\]|\\.)*"|[\d.]+)' % key, text)
            return json.loads(m.group(1)) if m else None

        ad.body = str(field("description") or "")
        # Fil d'Ariane : /catalog/3570-controllers/brand/272284-playstation?referrer=item-crumbs
        crumbs = re.findall(r'href="/catalog/(\d+)-([a-z0-9-]+)(?:/brand/\d+-([^?"]*))?'
                            r'\?referrer=item-crumbs', page.text)
        if crumbs:
            ad.vinted_catalog, ad.vinted_catalog_name = int(crumbs[-1][0]), crumbs[-1][1]
            ad.vinted_brand = next((c[2] for c in crumbs if c[2]), None)
        count = field("feedback_count")
        if count is not None:
            ad.attributes["rating_count"] = SimpleNamespace(value=str(count), value_label=None)
            ad.attributes["rating_score"] = SimpleNamespace(
                value=str(field("feedback_reputation") or 0), value_label=None)


def auto_buy_refusal(ad: SimpleNamespace, a: dict, product: dict, cfg: dict,
                     seen: dict) -> str | None:
    """Pourquoi NE PAS acheter automatiquement cette annonce ; None si toutes les règles passent.
    Règles volontairement strictes : au moindre doute, c'est toi qui décides."""
    rules = cfg.get("achat_auto") or {}
    if getattr(ad, "site", None) != "Vinted" or product["nom"] not in rules.get("produits", []):
        return "produit non concerné"
    if ad.price < rules.get("prix_min", 0):
        return f"prix sous {eur(rules['prix_min'])} : trop beau pour être vrai"
    if ad.price > product["achat"][1]:
        return f"au-dessus de ton achat max ({eur(product['achat'][1])})"
    if not getattr(ad, "vinted_catalog", None) or not getattr(ad, "vinted_brand", None):
        return "catégorie ou marque non lue sur la page"
    count = to_int(attr(ad, "rating_count"))
    if count < rules.get("avis_min", 0):
        return f"vendeur avec seulement {count} avis"
    stars = float(attr(ad, "rating_score") or 0) * 5
    if stars < rules.get("note_min", 0):
        return f"vendeur noté {stars:.1f}/5"
    if attr(ad, "condition") in rules.get("etats_refuses", []):
        return f"état « {attr_label(ad, 'condition')} »"
    if a["warn"]:
        return f"avertissement : {a['warn'][0]}"
    today = datetime.now().strftime("%Y-%m-%d")
    if sum(k.startswith(f"achat_auto:{today}:") for k in seen) >= rules.get("max_par_jour", 0):
        return f"déjà {rules.get('max_par_jour', 0)} achats auto aujourd'hui"
    return None


def auto_buy(ad: SimpleNamespace, a: dict, product: dict, cfg: dict, seen: dict) -> None:
    """Achat automatique (Vinted) selon le mode de config.yaml ; le résultat est ajouté à l'alerte."""
    rules = cfg.get("achat_auto") or {}
    mode = rules.get("mode", "off")
    if mode == "off" or product["nom"] not in rules.get("produits", []):
        return
    why = auto_buy_refusal(ad, a, product, cfg, seen)
    if why:
        log.info("  achat auto : non (%s)", why)
        a["auto_note"] = f"🤖 Achat auto{' (test)' if mode == 'test' else ''} : non, {html.escape(why)}"
        return
    seen[f"achat_auto:{datetime.now().strftime('%Y-%m-%d')}:{ad.id}"] = datetime.now().isoformat()
    if mode == "test":
        log.info("  achat auto (test) : l'aurait acheté")
        a["auto_note"] = "🤖 <b>Achat auto (test) : il l'aurait ACHETÉE</b> (rien n'a été acheté)"
        return
    try:
        vinted_buy(ad)
        log.info("  ACHAT AUTO réussi")
        a["auto_note"] = "🤖✅ <b>ACHETÉE AUTOMATIQUEMENT</b> : valide le paiement sur ton appli bancaire"
    except Exception as e:
        log.error("  achat auto raté (%s)", e)
        a["auto_note"] = f"🤖❌ <b>Achat auto raté</b> ({html.escape(str(e))}) : achète-la à la main, vite !"


def vinted_buy(ad: SimpleNamespace) -> None:
    """Achète l'annonce avec le compte Vinted (session dans bot.env).

    Parcours d'achat du site Vinted (lu dans son code, 03/11/2026) :
      1. POST /api/v2/purchases/checkout/build {"purchase_items": [{"id": <annonce>, "type": ...}]}
         -> numéro de commande (purchase_id)
      2. PUT /api/v2/purchases/<purchase_id>/checkout : livraison et point relais
      3. paiement : pas dans le code public, à lire sur la page /checkout avec la session du compte
    """
    raise NotImplementedError("paiement pas encore branché (session Vinted à ajouter)")


def vinted_product(product: dict, cfg: dict) -> dict:
    """Le produit avec les frais d'achat Vinted (protection acheteur + port) à la place de ceux de Leboncoin."""
    v = cfg["vinted"]
    port = v["port_accessoire"] if product.get("accessoire") else v["port"]
    return dict(product, rentabilite={**(product.get("rentabilite") or {}),
                                      "frais_achat_fixe": v["frais_fixe"] + port,
                                      "frais_achat_pct": v["frais_pct"]})


def run_vinted(vinted: Vinted, tg: Telegram, cfg: dict, seen: dict, market: MarketPrice,
               failed: list | None = None) -> None:
    """Même travail que run_once, sur Vinted. Pas de date sur Vinted : les annonces sont triées
    des plus récentes aux plus anciennes, et le bot retient celles qu'il a déjà vues."""
    per_page = cfg["vinted"].get("annonces_par_recherche", 24)
    for product in cfg["produits"]:
        if not product.get("mot_cle"):
            continue
        product = vinted_product(product, cfg)
        rent = rent_for(product, cfg)
        max_price = int(product["achat"][1] * (1 + rent["marge_negociation_achat_pct"] / 100))
        log.info("Recherche Vinted : %s (jusqu'à %s)", product["nom"], eur(max_price))
        ads = None
        for attempt in range(3):
            try:
                ads = vinted.search(product["mot_cle"], int(product.get("prix_min", 10)),
                                    max_price, per_page)
                break
            except Exception as e:
                log.error("Échec de la recherche Vinted, essai %d/3 (%s)", attempt + 1, e)
                vinted.reset()
                time.sleep(5)
        if ads is None:
            if failed is not None:
                failed.append(product["nom"] + " (Vinted)")
            continue
        new_ads = [ad for ad in ads if not already_seen(ad, seen)]
        # Tout est nouveau : il y en a peut-être d'autres sur la page suivante
        if ads and len(new_ads) == len(ads) == per_page and f"_vinted:{product['nom']}" in seen:
            try:
                more = vinted.search(product["mot_cle"], int(product.get("prix_min", 10)),
                                     max_price, per_page, page=2)
                new_ads += [ad for ad in more if not already_seen(ad, seen)]
            except Exception as e:
                log.warning("  page 2 Vinted impossible (%s)", e)
        log.info("  %d annonces Vinted, %d nouvelles", len(ads), len(new_ads))
        # Premier passage de ce produit sur Vinted : les annonces déjà en ligne sont
        # mémorisées sans être envoyées
        if f"_vinted:{product['nom']}" not in seen:
            for ad in new_ads:
                mark_seen(ad, seen)
            seen[f"_vinted:{product['nom']}"] = datetime.now().isoformat()
            save_seen(seen)
            continue
        for ad in new_ads:
            if already_seen(ad, seen):
                continue
            try:
                # D'abord sur le titre et le prix ; la page (description, avis) seulement si ça vaut le coup
                a = analyse(ad, product, market, cfg)
                if a:
                    # Description obligatoire : sans elle, impossible de vérifier qu'elle ne parle
                    # pas de virement. Pas marquée comme vue : réessayée au prochain passage
                    vinted.details(ad)
                    a = analyse(ad, product, market, cfg)
            except Exception as e:
                log.warning("  erreur d'analyse sur %s : %s", ad.url, e)
                continue
            if a:
                log.info("  %s %+.0f € : %s", a["label"], a["profit"], title_of(ad))
                auto_buy(ad, a, product, cfg, seen)
                if not tg.send(format_message(ad, a, product, cfg),
                               ad.images[0] if ad.images else None, buy_buttons(ad)):
                    continue
            mark_seen(ad, seen)
            save_seen(seen)
        time.sleep(3)
    save_seen(seen)


# ------------------------------------------------------------------- boucle

def run_once(client: lbc.Client, tg: Telegram, cfg: dict, seen: dict,
             market: MarketPrice, notify: bool = True, failed: list | None = None) -> int:
    """Fait un passage sur toutes les consoles ; renvoie le nombre de recherches réussies.
    failed : reçoit le nom des consoles dont la recherche a échoué.

    notify=False : marque les annonces du jour comme vues sans rien envoyer
    (premier lancement, ou redémarrage sur un hébergeur qui a perdu la mémoire).
    """
    # Annonces du jour... et celles de la fin de soirée, pour ne pas rater
    # une annonce publiée à 23h55 et trouvée au passage de 0h05
    now = datetime.now(PARIS)
    since = min(now.strftime("%Y-%m-%d 00:00:00"),
                (now - timedelta(minutes=max(30, 3 * cfg.get("intervalle_minutes", 10))))
                .strftime("%Y-%m-%d %H:%M:%S"))
    ok = 0
    for product in cfg["produits"]:
        rent = rent_for(product, cfg)
        max_price = int(product["achat"][1] * (1 + rent["marge_negociation_achat_pct"] / 100))
        log.info("Recherche : %s (jusqu'à %s)", product["nom"], eur(max_price))
        ads = None
        # Peu d'annonces par recherche (moins de données proxy) : en cherchant toutes les minutes,
        # les 12 dernières suffisent. Si elles sont toutes nouvelles (après une coupure, ou au
        # 1er passage), on recharge les 35 dernières pour ne rien rater.
        small = cfg.get("annonces_par_recherche", 35)
        for attempt in range(3):
            try:
                kwargs = dict(extra=True, sort=lbc.Sort.NEWEST,
                              price=[int(product.get("prix_min", 10)), max_price])
                ads = search(client, product, cfg, limit=small if notify else 35, **kwargs)
                if notify and len(ads) >= small and not any(already_seen(ad, seen) for ad in ads):
                    ads = search(client, product, cfg, limit=35, **kwargs)
                break
            except Exception as e:
                log.error("Échec de la recherche, essai %d/3 (%s)", attempt + 1, e)
                if isinstance(e, DatadomeError):
                    # Bloqué par DataDome : nouvelle connexion, nouvelle IP
                    try:
                        client = market.client = make_client()
                    except Exception as e2:
                        log.error("  nouvelle connexion impossible (%s)", e2)
                    time.sleep(5)
                # Coupure réseau (fréquente au 1er essai après une pause) : on garde la connexion
                # et son cookie, et on réessaie tout de suite (ça passe presque toujours)
        if ads is None:
            if failed is not None:
                failed.append(product["nom"])
            continue
        ok += 1
        # Seulement les annonces publiées aujourd'hui, jamais traitées
        # Date de mise en ligne OU de remontée : une annonce de la semaine dernière
        # remontée en tête aujourd'hui compte comme une annonce du jour
        todays = [ad for ad in ads if posted_at(ad) >= since]
        new_ads = [ad for ad in todays if not already_seen(ad, seen)]
        log.info("  %d annonces aujourd'hui, %d nouvelles", len(todays), len(new_ads))
        if not notify:
            for ad in new_ads:
                mark_seen(ad, seen)
            continue
        for ad in new_ads:
            if already_seen(ad, seen):  # republiée dans la même recherche
                continue
            try:
                a = analyse(ad, product, market, cfg)
            except Exception as e:
                # Pas marquée comme vue : elle sera réessayée au prochain passage
                log.warning("  erreur d'analyse sur %s : %s", ad.url, e)
                continue
            if a:
                log.info("  %s %+.0f € : %s", a["label"], a["profit"], title_of(ad))
                if not tg.send(format_message(ad, a, product, cfg),
                               ad.images[0] if ad.images else None, buy_buttons(ad)):
                    # Pas marquée comme vue : l'alerte sera renvoyée au prochain passage
                    continue
            mark_seen(ad, seen)
            save_seen(seen)
        time.sleep(3)
    save_seen(seen)
    return ok


def wake(client: lbc.Client) -> None:
    """Mini-recherche (1 annonce) pour rouvrir la connexion au proxy avant un passage."""
    for attempt in range(2):
        try:
            client.search(category=CATEGORY, limit=1)
            return
        except Exception as e:
            if attempt:
                log.info("  réveil de la connexion raté (%s)", e)


def run_loop(tg: Telegram, cfg: dict, once: bool = False, status: dict | None = None,
             max_minutes: float | None = None) -> int:
    """Boucle principale ; renvoie le nombre de recherches réussies au dernier passage.

    max_minutes : s'arrête avant cette durée (GitHub Actions coupe un job au bout de 6 h).
    """
    start = time.time()
    client = make_client()
    market = MarketPrice(client, cfg["rentabilite"].get("cache_marche_minutes", 120))
    vinted = Vinted() if (cfg.get("vinted") or {}).get("actif") else None
    seen = load_seen()
    notify = seen is not None
    if not notify:
        seen = {}
        log.info("Premier lancement : les annonces déjà en ligne sont mémorisées sans être envoyées")
    # Chaque produit peut avoir son propre délai (PS5 toutes les 3 min, le reste toutes les 10)
    default = cfg.get("intervalle_minutes", 10)
    every = {p["nom"]: p.get("intervalle_minutes", default) for p in cfg["produits"]}
    tick = min(every.values(), default=default)
    last_search: dict[str, float] = {}
    failures = 0          # passages d'affilée où aucune recherche n'a abouti
    alert_after = max(1, 60 // tick)  # environ 1 h
    degraded = False      # un échec a déjà été signalé, en attente d'un passage sans échec
    while True:
        ok = 0
        failed: list[str] = []
        pass_start = time.time()
        # Produits dont le délai est écoulé, à un demi-tour près : avec un tour de 3 min,
        # un produit à 10 min passe au bout de 9 min plutôt que d'attendre 12 min
        due = [p for p in cfg["produits"]
               if pass_start - last_search.get(p["nom"], 0) >= (every[p["nom"]] - tick / 2) * 60]
        for p in due:
            last_search[p["nom"]] = pass_start
        # La connexion est gardée d'un passage à l'autre : DataDome refuse souvent la première
        # recherche d'une connexion neuve (1 sur 4 la nuit), presque jamais les suivantes.
        # run_once en crée une nouvelle (dans market.client) seulement quand une recherche échoue.
        try:
            ok = run_once(market.client, tg, dict(cfg, produits=due), seen, market, notify, failed)
            notify = True
        except Exception as e:
            log.exception("Erreur inattendue : %s", e)
            failed = ["toutes les consoles"]
            market.client = make_client()
        client = market.client
        if vinted and notify:
            try:
                run_vinted(vinted, tg, dict(cfg, produits=due), seen, market, failed)
            except Exception as e:
                log.exception("Erreur inattendue sur Vinted : %s", e)
                failed.append("Vinted")
                vinted.reset()
        # Prévient sur Telegram au premier échec (une seule fois, pas à chaque passage),
        # puis après ~1 h de panne complète (proxy à court de crédit, blocage...), puis quand ça repart
        if failed and not degraded:
            tg.send(f"⚠️ <b>Recherche ratée</b> malgré 3 essais : {html.escape(', '.join(failed))}.\n"
                    "Les annonces seront rattrapées au prochain passage qui marche. "
                    "Je te préviens quand ça repart.")
            degraded = True
        if ok == 0:
            failures += 1
            if failures == alert_after:
                tg.send("⚠️ <b>Le bot n'arrive plus à joindre Leboncoin depuis environ 1 h.</b>\n"
                        "Vérifie ton crédit de proxy (DataImpulse). Le bot continue d'essayer "
                        "et te préviendra quand ça repart.")
        else:
            failures = 0
        if not failed and degraded:
            tg.send("✅ Les recherches marchent de nouveau, la surveillance reprend.")
            degraded = False
        # Rapport du marché, une fois par jour dès l'heure choisie. La date du dernier envoi
        # est gardée avec les annonces vues, pour survivre aux redémarrages (GitHub Actions).
        now = datetime.now(PARIS)
        hour = cfg.get("rapport_marche_heure")
        today = now.date().isoformat()
        if ok and hour is not None and now.hour >= hour and seen.get("_rapport_marche") != today:
            try:
                market_report(client, tg, cfg)
                seen["_rapport_marche"] = today
                save_seen(seen)
            except Exception as e:
                log.warning("Rapport du marché impossible : %s", e)
        if status is not None:
            status.update(last_pass=now.strftime("%d/%m %H:%M"),
                          searches_ok=ok, searches_total=len(due))
        if once:
            return ok
        if max_minutes is not None and (time.time() - start) / 60 + tick > max_minutes:
            log.info("Durée maximale atteinte, arrêt (un nouveau passage prendra le relais)")
            return ok
        # Le délai part du début du passage : un passage de 30 s ne décale pas le rythme
        wait = max(10, tick * 60 - (time.time() - pass_start))
        log.info("Prochain passage dans %d min %02d s", wait // 60, wait % 60)
        if os.environ.get("PROXY_URL") and wait > 30:
            # Après ~40 s sans rien faire, la 1re requête via le proxy échoue (15 s perdues) : on
            # réveille la connexion 20 s avant le passage, pour que la vraie recherche parte aussitôt
            time.sleep(wait - 20)
            woke = time.time()
            wake(market.client)
            time.sleep(max(0, 20 - (time.time() - woke)))
        else:
            time.sleep(wait)


def market_report(client: lbc.Client, tg: Telegram, cfg: dict) -> None:
    """Prix du marché actuel de chaque console comparé à ta grille."""
    market = MarketPrice(client, 0)
    day = datetime.now(PARIS).strftime("%d/%m")
    lines = [f"<b>☀️ Prix du marché du {day}</b> (consoles livrables, toute la France)", ""]
    for product in cfg["produits"]:
        rent = rent_for(product, cfg)
        prices = market.prices(product, cfg)
        name = html.escape(product["nom"])
        if len(prices) < 5:
            lines.append(f"<b>{name}</b> : pas assez d'annonces ({len(prices)})\n")
            continue
        med = statistics.median(prices)
        quick = prices[len(prices) // 4]
        low, high = product["revente"]
        # Même calcul que pour les alertes : revente rapide, prudente, marge minimum
        resale = resale_for(med, quick, product, rent)
        net = resale * (1 - rent["negociation_revente_pct"] / 100)
        max_buy = min(max_buy_for(net, margin_for(resale, rent), rent), product["achat"][1])
        verdict = "✔ conforme" if low * 0.9 <= quick <= high * 1.1 else (
            "⬆️ marché plus haut" if quick > high else "⬇️ marché plus bas")
        lines.append(
            f"<b>{name}</b> ({len(prices)} annonces)\n"
            f"  vente rapide {eur(quick)} · médiane {eur(med)}\n"
            f"  ta revente {eur(low)}–{eur(high)} → {verdict}\n"
            f"  achat max du bot : {eur(max_buy)} (ta grille : {eur(product['achat'][1])})\n"
        )
    tg.send("\n".join(lines))


def telegram_from_config(cfg: dict, dry_run: bool = False) -> Telegram:
    # Sur un hébergeur, le token et le chat_id viennent des variables d'environnement
    token = os.environ.get("TELEGRAM_TOKEN") or cfg["telegram"]["token"]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg["telegram"]["chat_id"]
    return Telegram(token, str(chat_id), dry_run=dry_run)


def print_chat_id(token: str) -> None:
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in r.get("result", []) if "message" in u}
    if not chats:
        print("Aucun message reçu. Envoie d'abord un message à ton bot sur Telegram, puis relance.")
    for cid, name in chats.items():
        print(f"chat_id = {cid}   ({name})")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--marche", action="store_true")
    p.add_argument("--chat-id", action="store_true")
    p.add_argument("--test", action="store_true")
    args = p.parse_args()

    cfg = load_config()
    if args.chat_id:
        print_chat_id(telegram_from_config(cfg).token)
        return
    tg = telegram_from_config(cfg, dry_run=args.dry_run)
    if args.test:
        tg.send("✅ Le bot Leboncoin est bien connecté !")
        return

    if args.marche:
        market_report(make_client(), tg, cfg)
        return

    if run_loop(tg, cfg, once=args.once) == 0:
        sys.exit("Aucune recherche n'a abouti (Leboncoin bloque peut-être cette connexion)")


if __name__ == "__main__":
    main()
