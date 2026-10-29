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
        logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
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


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    limit = (datetime.now() - timedelta(days=60)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= limit}
    SEEN_FILE.write_text(json.dumps(seen), encoding="utf-8")


def attr(ad: lbc.Ad, key: str) -> str | None:
    a = ad.attributes.get(key)
    return a.value if a else None


def attr_label(ad: lbc.Ad, key: str) -> str | None:
    a = ad.attributes.get(key)
    return a.value_label if a else None


def eur(value: float) -> str:
    return f"{value:,.0f} €".replace(",", " ")


def has_word(text: str, word: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text) is not None


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
        if has_word(head, w):
            return f"accessoire seul (« {w} »)"
    weight = int(attr(ad, "estimated_parcel_weight") or 0)
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
    for w in cfg.get("mots_interdits", []):
        if has_word(text, w):
            return f"console à problème (« {w} »)"
    return None


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
            ads = search(self.client, product, cfg, limit=100)
            prices = sorted(a.price for a in ads if a.price and not rejection(a, product, cfg))
            self.cache[name] = (time.time(), prices)
            time.sleep(1.5)
        return self.cache[name][1]

    def median(self, product: dict, cfg: dict) -> tuple[float | None, int]:
        prices = self.prices(product, cfg)
        if len(prices) < 6:
            return None, len(prices)
        return statistics.median(prices), len(prices)


# ----------------------------------------------------------------- analyse

def costs_for(price: float, rent: dict) -> float:
    return rent["frais_achat_fixe"] + price * rent["frais_achat_pct"] / 100


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
    repairs = {w: c for w, c in (cfg.get("frais_reparation") or {}).items() if w in text}
    repair_cost = sum(repairs.values())
    if repairs:
        warn.append("À prévoir : " + ", ".join(repairs) + f" (+{eur(repair_cost)})")
    good += [f"« {w} »" for w in cfg.get("mots_positifs", []) if w in text]
    if attr(ad, "condition") == "etatsatisfaisant":
        warn.append("État satisfaisant (traces d'usure)")

    # --- prix du marché : en direct, comparé à ta fourchette de revente
    low, high = product["revente"]
    mid = (low + high) / 2
    live, n = market.median(product, cfg)
    if live:
        if live < low * 0.9:
            market_note = f"⬇️ plus bas que ta revente prévue ({eur(low)}–{eur(high)})"
        elif live > high * 1.1:
            market_note = f"⬆️ plus haut que ta revente prévue ({eur(low)}–{eur(high)})"
        else:
            market_note = f"✔ conforme à ta revente prévue ({eur(low)}–{eur(high)})"
        resale = min(live, mid) if rent.get("prudent", True) else live
    else:
        market_note = f"pas assez d'annonces ({n}), estimation via ta grille"
        resale = mid

    # --- rentabilité
    net_resale = resale * (1 - rent["negociation_revente_pct"] / 100)
    costs = costs_for(price, rent) + repair_cost
    profit = net_resale - price - costs
    # achat max : le prix qui laisse encore la marge minimum
    max_buy = (net_resale - rent["frais_achat_fixe"] - repair_cost - rent["marge_min"]) \
        / (1 + rent["frais_achat_pct"] / 100)
    max_buy = min(max_buy, product["achat"][1])
    if price > max_buy * (1 + rent["marge_negociation_achat_pct"] / 100):
        log.info("  trop cher %s (max %s) : %s", eur(price), eur(max_buy), title_of(ad))
        return None

    if price < resale * rent.get("prix_suspect_pct", 55) / 100:
        warn.insert(0, "Prix anormalement bas : arnaque possible. Paie UNIQUEMENT via le "
                       "paiement sécurisé Leboncoin, jamais par virement ou en dehors du site")

    score, count = attr(ad, "rating_score"), attr(ad, "rating_count")
    if count and int(count) > 0:
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

    return {
        "label": label,
        "live": live,
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

    def send(self, text: str, photo: str | None = None) -> None:
        if self.dry_run:
            print("\n" + html.unescape(re.sub(r"<[^>]+>", "", text)) + "\n" + "-" * 60)
            return
        if photo and len(text) <= 1024:
            r = requests.post(f"{self.api}/sendPhoto", data={
                "chat_id": self.chat_id, "photo": photo,
                "caption": text, "parse_mode": "HTML"}, timeout=30)
            if r.ok:
                return
        r = requests.post(f"{self.api}/sendMessage", data={
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true"}, timeout=30)
        if not r.ok:
            log.error("Erreur Telegram : %s", r.text)


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
        market = f"📊 Marché : {eur(a['live'])} (médiane de {a['count']} annonces) {a['market_note']}"
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
    today = datetime.now(PARIS).date().isoformat()
    rent = cfg["rentabilite"]
    ok = 0
    for product in cfg["produits"]:
        max_price = int(product["achat"][1] * (1 + rent["marge_negociation_achat_pct"] / 100))
        log.info("Recherche : %s (jusqu'à %s)", product["nom"], eur(max_price))
        try:
            ads = search(client, product, cfg, limit=35, sort=lbc.Sort.NEWEST,
                         price=[int(product.get("prix_min", 10)), max_price])
        except Exception as e:
            log.error("Échec de la recherche (%s)", e)
            continue
        ok += 1
        # Seulement les annonces publiées aujourd'hui, jamais traitées
        todays = [ad for ad in ads if (ad.first_publication_date or "")[:10] >= today]
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
            mark_seen(ad, seen)
            if a:
                log.info("  %s %+.0f € : %s", a["label"], a["profit"], title_of(ad))
                tg.send(format_message(ad, a, product, cfg),
                        ad.images[0] if ad.images else None)
            save_seen(seen)
        time.sleep(3)
    save_seen(seen)
    return ok


def run_loop(tg: Telegram, cfg: dict, once: bool = False, status: dict | None = None) -> int:
    """Boucle principale ; renvoie le nombre de recherches réussies au dernier passage."""
    client = make_client()
    market = MarketPrice(client, cfg["rentabilite"].get("cache_marche_minutes", 120))
    notify = SEEN_FILE.exists()
    seen = load_seen()
    if not notify:
        log.info("Premier lancement : les annonces déjà en ligne sont mémorisées sans être envoyées")
    while True:
        ok = 0
        try:
            ok = run_once(client, tg, cfg, seen, market, notify)
            notify = True
        except Exception as e:
            log.exception("Erreur inattendue : %s", e)
            client = make_client()
            market = MarketPrice(client, cfg["rentabilite"].get("cache_marche_minutes", 120))
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
        if once:
            return ok
        minutes = cfg.get("intervalle_minutes", 10)
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
        cheap = prices[len(prices) // 4]
        net = med * (1 - rent["negociation_revente_pct"] / 100)
        max_buy = (net - rent["frais_achat_fixe"] - rent["marge_min"]) / (1 + rent["frais_achat_pct"] / 100)
        low, high = product["revente"]
        verdict = "✔ conforme" if low * 0.9 <= med <= high * 1.1 else (
            "⬆️ marché plus haut" if med > high else "⬇️ marché plus bas")
        lines.append(
            f"<b>{name}</b> ({len(prices)} annonces)\n"
            f"  médiane {eur(med)} · 25 % les moins chères ≤ {eur(cheap)}\n"
            f"  ta revente {eur(low)}–{eur(high)} → {verdict}\n"
            f"  achat max recalculé {eur(max_buy)} (ta grille : {eur(product['achat'][1])})\n"
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
