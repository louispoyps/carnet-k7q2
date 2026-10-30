"""
Bot achat-revente de consoles Leboncoin -> Telegram.

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
import re
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import lbc
import yaml
from curl_cffi import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
SECRET_FILE = BASE_DIR / "secret.yaml"  # token Telegram local, jamais envoyé sur GitHub
SEEN_FILE = BASE_DIR / "annonces_vues.json"

PARIS = ZoneInfo("Europe/Paris")
CATEGORY = lbc.Category.ELECTRONIQUE_CONSOLES

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
    # Identifiants laissés encodés (%40...) : curl les décode lui-même
    return lbc.Client(proxy=lbc.Proxy(host=u.hostname, port=u.port, username=u.username,
                                      password=u.password, scheme=u.scheme))


def search(client: lbc.Client, product: dict, cfg: dict, limit: int, **kwargs) -> list[lbc.Ad]:
    """Annonces de consoles (pas d'accessoires) livrables pour ce produit."""
    result = client.search(
        category=CATEGORY, limit=limit,
        video_game_type=["console"], console_model=product["modeles"],
        shipping_type=cfg["modes_envoi"], **kwargs,
    )
    return result.ads


def rejection(ad: lbc.Ad, product: dict, cfg: dict) -> str | None:
    """Motif de rejet d'une annonce (hors prix), ou None si elle est acceptable."""
    title = title_of(ad).lower()
    text = f"{title} {ad.body or ''}".lower()
    for w in cfg.get("titre_exclut", []) + product.get("titre_exclut", []):
        if has_word(title, w):
            return f"titre « {w} »"
    first = re.sub(r"^[^\w]+", "", title).split(" ", 1)[0]
    if first in cfg.get("titre_debut_exclut", []):
        return f"accessoire (titre commence par « {first} »)"
    head = re.split(r"\+|\bavec\b|&|\bet\b", title, maxsplit=1)[0]
    for w in cfg.get("mots_accessoires", []):
        for m in re.finditer(r"(?<!\w)" + re.escape(w) + r"(?!\w)", head):
            # "2 jeux", "3 manettes", "1to de jeux" : c'est un lot avec la console
            if not BUNDLE_BEFORE.search(head[:m.start()]):
                return f"accessoire seul (« {w} »)"
    weight = to_int(attr(ad, "estimated_parcel_weight"))
    if 200 <= weight < product.get("poids_min_g", 0):  # < 200 g : poids non renseigné
        return f"colis trop léger pour la console ({weight} g)"
    if ad.price and ad.price < product.get("prix_min", 0):
        return f"prix trop bas pour une vraie console ({eur(ad.price)})"
    if attr(ad, "video_game_type") not in (None, "console"):
        return "pas une console"
    if attr(ad, "condition") == "pourpieces":
        return "vendue pour pièces"
    if attr(ad, "shippable") != "true":
        return "pas de livraison"
    for w in cfg.get("mots_main_propre", []):
        if w in text:
            return f"main propre uniquement (« {w} »)"
    return None


def problem_words(ad: lbc.Ad, cfg: dict) -> list[str]:
    """Mots inquiétants présents dans l'annonce (sans être niés) : signalés dans l'alerte."""
    text = f"{title_of(ad)} {ad.body or ''}".lower()
    return [w for w in cfg.get("mots_a_verifier", []) if mentions(text, w)]


def fingerprint(ad: lbc.Ad) -> str:
    """Empreinte de l'annonce, pour reconnaître une annonce republiée sous un autre numéro."""
    title = re.sub(r"\W+", " ", title_of(ad).lower()).strip()
    return f"fp:{title}|{ad.location.zipcode}|{attr(ad, 'console_model')}"


def already_seen(ad: lbc.Ad, seen: dict) -> bool:
    return str(ad.id) in seen or fingerprint(ad) in seen


def mark_seen(ad: lbc.Ad, seen: dict) -> None:
    now = datetime.now().isoformat()
    seen[str(ad.id)] = now
    seen[fingerprint(ad)] = now


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
                batch = search(self.client, product, cfg, limit=100, page=page)
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
    rent = cfg["rentabilite"]
    price = ad.price
    if not price:
        return None
    reason = rejection(ad, product, cfg)
    if reason:
        log.info("  ignorée (%s) : %s", reason, title_of(ad))
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
                    + " — lis la description et demande au vendeur si la console marche")
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
        resale = min(quick, mid) if rent.get("prudent", True) else quick
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

    if price < resale * rent.get("prix_suspect_pct", 55) / 100:
        warn.insert(0, "Prix anormalement bas : arnaque possible. Paie UNIQUEMENT via le "
                       "paiement sécurisé Leboncoin, jamais par virement ou en dehors du site")

    score, count = attr(ad, "rating_score"), attr(ad, "rating_count")
    if to_int(count) > 0:
        stars = float(score or 0) * 5
        (good if stars >= 4.5 else warn).append(f"Vendeur noté {stars:.1f}/5 ({count} avis)")
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

    def send(self, text: str, photo: str | None = None) -> bool:
        """Envoie le message ; renvoie False s'il n'a pas pu partir."""
        if self.dry_run:
            print("\n" + html.unescape(re.sub(r"<[^>]+>", "", text)) + "\n" + "-" * 60)
            return True
        if photo and len(text) <= 1024 and self._post("sendPhoto", {
                "chat_id": self.chat_id, "photo": photo, "caption": text, "parse_mode": "HTML"}):
            return True
        # Sans photo (photo refusée ou texte trop long pour une légende)
        if self._post("sendMessage", {
                "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": "true"}):
            return True
        # Dernier essai en texte simple, si c'est la mise en forme que Telegram refuse
        return self._post("sendMessage", {
            "chat_id": self.chat_id, "text": html.unescape(re.sub(r"<[^>]+>", "", text)),
            "disable_web_page_preview": "true"})


def format_message(ad: lbc.Ad, a: dict, product: dict, cfg: dict) -> str:
    e = html.escape
    rent = cfg["rentabilite"]
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
    lines = [
        f"<b>{a['label']} · {e(product['nom'])}</b>",
        f"🎮 {e(title_of(ad))}",
        f"📋 {e(details)}",
        f"📍 {e(ad.location.city or '')} ({e(ad.location.zipcode or '')})",
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
    lines += ["", f'<a href="{ad.url}">Voir l\'annonce</a>']
    return "\n".join(lines)


# ------------------------------------------------------------------- boucle

def run_once(client: lbc.Client, tg: Telegram, cfg: dict, seen: dict,
             market: MarketPrice, notify: bool = True) -> int:
    """Fait un passage sur toutes les consoles ; renvoie le nombre de recherches réussies.

    notify=False : marque les annonces du jour comme vues sans rien envoyer
    (premier lancement, ou redémarrage sur un hébergeur qui a perdu la mémoire).
    """
    # Annonces du jour... et celles de la fin de soirée, pour ne pas rater
    # une annonce publiée à 23h55 et trouvée au passage de 0h05
    now = datetime.now(PARIS)
    since = min(now.strftime("%Y-%m-%d 00:00:00"),
                (now - timedelta(minutes=3 * cfg.get("intervalle_minutes", 10)))
                .strftime("%Y-%m-%d %H:%M:%S"))
    rent = cfg["rentabilite"]
    ok = 0
    for product in cfg["produits"]:
        max_price = int(product["achat"][1] * (1 + rent["marge_negociation_achat_pct"] / 100))
        log.info("Recherche : %s (jusqu'à %s)", product["nom"], eur(max_price))
        ads = None
        for attempt in range(3):
            try:
                ads = search(client, product, cfg, limit=35, sort=lbc.Sort.NEWEST,
                             price=[int(product.get("prix_min", 10)), max_price])
                break
            except Exception as e:
                log.error("Échec de la recherche, essai %d/3 (%s)", attempt + 1, e)
                # Nouvelle connexion = nouvelle IP du proxy ; souvent suffisant
                client = market.client = make_client()
                time.sleep(5)
        if ads is None:
            continue
        ok += 1
        # Seulement les annonces publiées aujourd'hui, jamais traitées
        todays = [ad for ad in ads if (ad.first_publication_date or "") >= since]
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
                               ad.images[0] if ad.images else None):
                    # Pas marquée comme vue : l'alerte sera renvoyée au prochain passage
                    continue
            mark_seen(ad, seen)
            save_seen(seen)
        time.sleep(3)
    save_seen(seen)
    return ok


def run_loop(tg: Telegram, cfg: dict, once: bool = False, status: dict | None = None,
             max_minutes: float | None = None) -> int:
    """Boucle principale ; renvoie le nombre de recherches réussies au dernier passage.

    max_minutes : s'arrête avant cette durée (GitHub Actions coupe un job au bout de 6 h).
    """
    start = time.time()
    client = make_client()
    market = MarketPrice(client, cfg["rentabilite"].get("cache_marche_minutes", 120))
    seen = load_seen()
    notify = seen is not None
    if not notify:
        seen = {}
        log.info("Premier lancement : les annonces déjà en ligne sont mémorisées sans être envoyées")
    failures = 0          # passages d'affilée où aucune recherche n'a abouti
    alert_after = max(1, 60 // cfg.get("intervalle_minutes", 10))  # environ 1 h
    while True:
        ok = 0
        # Connexion neuve à chaque passage : une connexion proxy coincée ne bloque plus des heures
        client = market.client = make_client()
        try:
            ok = run_once(client, tg, cfg, seen, market, notify)
            notify = True
        except Exception as e:
            log.exception("Erreur inattendue : %s", e)
            client = make_client()
            market = MarketPrice(client, cfg["rentabilite"].get("cache_marche_minutes", 120))
        # Prévient sur Telegram si le bot ne joint plus Leboncoin (proxy à court de crédit,
        # blocage...), puis quand ça repart
        if ok == 0:
            failures += 1
            if failures == alert_after:
                tg.send("⚠️ <b>Le bot n'arrive plus à joindre Leboncoin depuis environ 1 h.</b>\n"
                        "Vérifie ton crédit de proxy (DataImpulse). Le bot continue d'essayer "
                        "et te préviendra quand ça repart.")
        else:
            if failures >= alert_after:
                tg.send("✅ Le bot joint de nouveau Leboncoin, la surveillance reprend.")
            failures = 0
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
                          searches_ok=ok, searches_total=len(cfg["produits"]))
        minutes = cfg.get("intervalle_minutes", 10)
        if once:
            return ok
        if max_minutes is not None and (time.time() - start) / 60 + minutes > max_minutes:
            log.info("Durée maximale atteinte, arrêt (un nouveau passage prendra le relais)")
            return ok
        log.info("Prochain passage dans %d min", minutes)
        time.sleep(minutes * 60)


def market_report(client: lbc.Client, tg: Telegram, cfg: dict) -> None:
    """Prix du marché actuel de chaque console comparé à ta grille."""
    rent = cfg["rentabilite"]
    market = MarketPrice(client, 0)
    day = datetime.now(PARIS).strftime("%d/%m")
    lines = [f"<b>☀️ Prix du marché du {day}</b> (consoles livrables, toute la France)", ""]
    for product in cfg["produits"]:
        prices = market.prices(product, cfg)
        name = html.escape(product["nom"])
        if len(prices) < 5:
            lines.append(f"<b>{name}</b> : pas assez d'annonces ({len(prices)})\n")
            continue
        med = statistics.median(prices)
        quick = prices[len(prices) // 4]
        low, high = product["revente"]
        # Même calcul que pour les alertes : revente rapide, prudente, marge minimum
        resale = min(quick, (low + high) / 2) if rent.get("prudent", True) else quick
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
