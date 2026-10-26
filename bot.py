"""
Bot achat-revente Leboncoin -> Telegram.

Pour chaque modèle de ta grille (config.yaml), le bot :
  - cherche les nouvelles annonces à prix bas,
  - calcule le prix du marché en direct (annonces comparables),
  - le compare à ta fourchette de prix,
  - vérifie l'historique (rapport Autoviza fourni par Leboncoin),
  - calcule ton bénéfice estimé et t'envoie les bonnes affaires sur Telegram.

Usage :
    python bot.py               boucle infinie
    python bot.py --once        un seul passage
    python bot.py --dry-run     affiche dans la console au lieu d'envoyer
    python bot.py --marche      état du marché de chaque modèle vs ta grille
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

import lbc
import yaml
from bs4 import BeautifulSoup
from curl_cffi import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.yaml"
SECRET_FILE = BASE_DIR / "secret.yaml"  # token Telegram local, jamais envoyé sur GitHub
SEEN_FILE = BASE_DIR / "annonces_vues.json"

FUELS = {"essence": "1", "diesel": "2", "hybride": "6", "electrique": "4", "électrique": "4"}

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


def to_int(value) -> int | None:
    try:
        return int(str(value).replace(" ", ""))
    except (TypeError, ValueError):
        return None


def normalize(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").replace(" ", " ").split())


def eur(value: float) -> str:
    return f"{value:,.0f} €".replace(",", " ")


def has_word(text: str, word: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text) is not None


def round_down(value: float, step: int = 50) -> int:
    return int(value // step * step)


def locations(cfg: dict) -> list[lbc.Department] | None:
    wanted = {str(d).lstrip("0") for d in cfg.get("departements") or []}
    found = [d for d in lbc.Department if len(d.value) == 4 and d.value[2].lstrip("0") in wanted]
    return found or None


def model_filters(model: dict) -> dict:
    kwargs = {
        "u_car_brand": [model["marque"]],
        "u_car_model": [model["modele"]],
        "regdate": [int(y) for y in model["annees"]],
        "mileage": [int(k) for k in model["km"]],
    }
    if model.get("carburant"):
        kwargs["fuel"] = [FUELS[model["carburant"].lower()]]
    return kwargs


def matches_power(ad: lbc.Ad, model: dict) -> bool:
    target = model.get("puissance_ch")
    hp = to_int(attr(ad, "horse_power_din"))
    return not target or hp is None or abs(hp - target) <= 5


# ------------------------------------------------------------- prix marché

class MarketPrice:
    """Prix médian d'annonces comparables, avec cache."""

    def __init__(self, client: lbc.Client):
        self.client = client
        self.cache: dict[tuple, list[tuple[int, float]]] = {}

    def _search(self, **kwargs) -> list[tuple[int, float]]:
        key = tuple(sorted((k, tuple(v)) for k, v in kwargs.items()))
        if key not in self.cache:
            result = self.client.search(
                category=lbc.Category.VEHICULES_VOITURES, limit=100, **kwargs
            )
            self.cache[key] = [(a.id, a.price) for a in result.ads if a.price and a.price > 500]
            time.sleep(1.5)
        return self.cache[key]

    def for_ad(self, ad: lbc.Ad, min_count: int = 6) -> tuple[float | None, int]:
        """Médiane des annonces de même modèle, année ±1, km proche (toute la France)."""
        year = to_int(attr(ad, "regdate"))
        if not year:
            return None, 0
        km = to_int(attr(ad, "mileage"))
        base = {"u_car_brand": [attr(ad, "u_car_brand")],
                "u_car_model": [attr(ad, "u_car_model")]}
        if km:
            base["mileage"] = [int(km * 0.7), int(km * 1.3) + 10000]
        fuel = attr(ad, "fuel")
        prices = []
        for fuel_f, delta in ((fuel, 1), (fuel, 2), (None, 2)):
            kwargs = dict(base, regdate=[year - delta, year + delta])
            if fuel_f:
                kwargs["fuel"] = [fuel_f]
            prices = [p for i, p in self._search(**kwargs) if i != ad.id]
            if len(prices) >= min_count:
                return statistics.median(prices), len(prices)
        return None, len(prices)

    def for_model(self, model: dict) -> list[float]:
        return [p for _, p in self._search(**model_filters(model))]


# ------------------------------------------------------ rapport historique

def check_history(url: str, listed_km: int | None) -> dict:
    """Lit le rapport Autoviza et en extrait les points importants."""
    r = requests.get(url, impersonate="chrome", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = normalize(soup.get_text(" "))

    info = {
        "owners": None,
        "accidents": 0,
        "atypical": [],
        "pro_usage": [],
        "km_readings": [],
        "km_incoherent": False,
        "damaged": False,
    }

    m = re.search(r"transmise par (\d+) propriétaires?", text)
    if m:
        info["owners"] = int(m.group(1))

    hist = text
    m = re.search(r"Historique du véhicule(.*?)(Services vendeur|A propos des données)", text)
    if m:
        hist = m.group(1)

    info["accidents"] = len(re.findall(r"Type de sinistre", hist))
    if info["accidents"] == 0 and "partenaire expertise" in hist:
        info["accidents"] = 1

    m = re.search(r"(\d+) opérations? atypiques? détectées?\s*\.?(.*?)Vérification", text)
    if m and int(m.group(1)) > 0:
        info["atypical"] = [s.strip(" .") for s in m.group(2).split("•")
                            if s.strip(" .") and not s.strip().startswith("Aucune")]

    for label in ("Location courte-durée", "Démonstration", "Auto-école",
                  "Taxi/VTC", "Administratif", "Autre"):
        m = re.search(re.escape(label) + r" (Non détecté|Détecté|détecté)", text)
        if m and m.group(1) != "Non détecté":
            info["pro_usage"].append(label)

    # Relevés km (affichés du plus récent au plus ancien)
    readings = [to_int(k) for k in re.findall(r"(?<!\d)(\d{1,3}(?: \d{3})+|\d{4,6}) km", hist)]
    readings = [k for k in readings if k]
    info["km_readings"] = readings
    if readings:
        if listed_km is not None and max(readings) > listed_km * 1.05 + 5000:
            info["km_incoherent"] = True
        if any(newer < older - 1000 for newer, older in zip(readings, readings[1:])):
            info["km_incoherent"] = True

    if re.search(r"gravement endommagé|procédure VE|VGE|VEI", hist, re.I):
        info["damaged"] = True
    return info


def history_verdict(ad: lbc.Ad, crit: dict, good: list, warn: list) -> str | None:
    """Ajoute les points d'historique ; renvoie un motif de rejet ou None."""
    km = to_int(attr(ad, "mileage"))
    url = attr(ad, "vehicle_history_report_public_url")
    h = None
    if url:
        try:
            h = check_history(url, km)
        except Exception as e:
            log.warning("  rapport illisible (%s) : %s", e, url)
    if h is None:
        if crit.get("exiger_rapport"):
            return "pas de rapport d'historique"
        warn.append("Pas de rapport d'historique : demande le code HistoVec au vendeur")
        return None

    if h["owners"] is not None:
        if h["owners"] > crit.get("proprietaires_max", 99):
            return f"{h['owners']} propriétaires"
        (good if h["owners"] <= 3 else warn).append(f"{h['owners']} propriétaire(s)")
    if h["accidents"]:
        if crit.get("rejeter_si_sinistre", True):
            return "sinistre déclaré"
        warn.append(f"{h['accidents']} sinistre(s) / expertise(s)")
    else:
        good.append("Aucun sinistre connu")
    if h["km_incoherent"]:
        if crit.get("rejeter_si_km_incoherent", True):
            return f"kilométrage incohérent {h['km_readings']}"
        warn.append(f"Kilométrage incohérent ! relevés : {h['km_readings']}")
    elif h["km_readings"]:
        good.append(f"Kilométrage cohérent ({len(h['km_readings'])} relevé(s))")
    if h["damaged"]:
        if crit.get("rejeter_si_vehicule_endommage", True):
            return "procédure véhicule endommagé"
        warn.append("Procédure véhicule endommagé (VE/VGE)")
    if h["pro_usage"]:
        if crit.get("rejeter_si_usage_pro"):
            return "usage pro " + ", ".join(h["pro_usage"])
        warn.append("Usage pro : " + ", ".join(h["pro_usage"]))
    warn.extend(h["atypical"])
    return None


# ----------------------------------------------------------------- analyse

def analyse(ad: lbc.Ad, model: dict, market: MarketPrice, cfg: dict) -> dict | None:
    """Calcule la rentabilité ; renvoie l'analyse si l'affaire vaut le coup, sinon None."""
    rent = cfg["rentabilite"]
    price = ad.price
    if not price or not matches_power(ad, model):
        return None

    title = html.unescape(ad.subject or "").lower()
    if model.get("titre_contient") and not any(has_word(title, w) for w in model["titre_contient"]):
        log.info("  ignoré (titre hors modèle) : %s", ad.subject)
        return None
    if any(has_word(title, w) for w in model.get("titre_exclut", [])):
        log.info("  ignoré (autre modèle) : %s", ad.subject)
        return None

    text = f"{ad.subject} {ad.body or ''}".lower()
    bad = [w for w in cfg.get("mots_interdits", []) if has_word(text, w)]
    if bad:
        log.info("  rejet (mots %s) : %s", bad, ad.subject)
        return None

    good, warn = [], []

    # --- frais
    damage = attr(ad, "vehicle_damage") or ""
    if damage == "damaged" and cfg["historique"].get("rejeter_si_vehicule_endommage", True):
        log.info("  rejet (endommagé) : %s", ad.subject)
        return None
    if damage:
        text += " " + (attr_label(ad, "vehicle_damage") or "").lower()
    costs = rent["frais_fixes"]
    extra = {w: c for w, c in (cfg.get("frais_travaux") or {}).items() if w in text}
    if extra:
        costs += max(extra.values())
        warn.append("Travaux mentionnés : " + ", ".join(extra) + f" (+{eur(max(extra.values()))})")
    good += [f"« {w} »" for w in cfg.get("mots_positifs", []) if w in text]

    # --- prix du marché : en direct, comparé à ta grille
    grid_low, grid_high = model["prix_moyen"]
    grid_mid = (grid_low + grid_high) / 2
    live, n = market.for_ad(ad)
    if live:
        if live < grid_low * 0.9:
            market_note = f"⬇️ plus bas que ta grille ({eur(grid_low)}–{eur(grid_high)})"
        elif live > grid_high * 1.1:
            market_note = f"⬆️ plus haut que ta grille ({eur(grid_low)}–{eur(grid_high)})"
        else:
            market_note = f"✔ conforme à ta grille ({eur(grid_low)}–{eur(grid_high)})"
        resale = min(live, grid_mid) if rent.get("prudent", True) else live
    else:
        market_note = f"pas assez d'annonces ({n}), estimation via ta grille"
        resale = grid_mid

    # --- rentabilité
    net_resale = resale * (1 - rent["negociation_revente_pct"] / 100)
    profit = net_resale - costs - price
    max_buy = net_resale - costs - rent["marge_min"]
    if rent.get("respecter_grille", True):
        max_buy = min(max_buy, model["achat_max"])
    negotiable_up_to = max_buy * (1 + rent["marge_negociation_achat_pct"] / 100)

    if price > negotiable_up_to:
        log.info("  trop cher %s (max %s) : %s", eur(price), eur(max_buy), ad.subject)
        return None

    ref = live or grid_mid
    if price < ref * rent.get("prix_suspect_pct", 50) / 100:
        warn.insert(0, "Prix anormalement bas : arnaque possible ou gros défaut caché !")

    reason = history_verdict(ad, cfg["historique"], good, warn)
    if reason:
        log.info("  rejet (%s) : %s", reason, ad.subject)
        return None

    vise_low, vise_high = model["achat_vise"]
    if price <= max_buy and price <= vise_high:
        level, label = 3, "🔥🔥 AFFAIRE EN OR"
        offer = min(price, max(vise_low, round_down(price * 0.93)))
    elif price <= max_buy:
        level, label = 2, "🔥 BONNE AFFAIRE"
        offer = max(vise_high, round_down(price * 0.92))
    else:
        level, label = 1, "💬 À NÉGOCIER"
        offer = round_down(max_buy)
    offer = min(offer, price)

    return {
        "label": label,
        "level": level,
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
        "offer_profit": net_resale - costs - offer,
        "good": good,
        "warn": warn,
    }


# ---------------------------------------------------------------- telegram

class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
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


def format_message(ad: lbc.Ad, a: dict, model: dict, cfg: dict) -> str:
    e = html.escape
    rent = cfg["rentabilite"]
    km = to_int(attr(ad, "mileage"))
    details = " · ".join(filter(None, [
        attr(ad, "regdate"),
        f"{km:,} km".replace(",", " ") if km else None,
        attr_label(ad, "fuel"),
        attr_label(ad, "gearbox"),
        attr_label(ad, "horse_power_din"),
    ]))
    if a["live"]:
        market = f"📊 Marché : {eur(a['live'])} (médiane de {a['count']} annonces) {a['market_note']}"
    else:
        market = f"📊 Marché : {a['market_note']}"
    lines = [
        f"<b>{a['label']} · {e(model['nom'])}</b>",
        f"🚗 {e(html.unescape(ad.subject or ''))}",
        f"📋 {e(details)}",
        f"📍 {e(ad.location.city or '')} ({e(ad.location.zipcode or '')})",
        "",
        f"💶 Prix : <b>{eur(ad.price)}</b>  (ton achat visé {eur(model['achat_vise'][0])}–"
        f"{eur(model['achat_vise'][1])}, max {eur(a['max_buy'])})",
        e(market),
        f"🔁 Revente : {eur(a['resale'])} → {eur(a['net_resale'])} après "
        f"{rent['negociation_revente_pct']} % de négo",
        f"🧾 Frais : {eur(a['costs'])}",
        f"💰 <b>Bénéfice au prix affiché : {a['profit']:+,.0f} €</b> "
        f"(rentabilité {a['roi']:.0f} %)".replace(",", " "),
        f"🤝 Propose <b>{eur(a['offer'])}</b> → bénéfice {a['offer_profit']:+,.0f} €".replace(",", " "),
    ]
    if a["good"] or a["warn"]:
        lines.append("")
    lines += [f"✅ {e(g)}" for g in a["good"]]
    lines += [f"⚠️ {e(w)}" for w in a["warn"]]
    links = [f'<a href="{ad.url}">Voir l\'annonce</a>']
    report = attr(ad, "vehicle_history_report_public_url")
    if report:
        links.append(f'<a href="{report}">Historique</a>')
    lines += ["", " | ".join(links)]
    return "\n".join(lines)


# ------------------------------------------------------------------- boucle

def run_once(client: lbc.Client, tg: Telegram, cfg: dict, seen: dict) -> int:
    """Fait un passage sur tous les modèles ; renvoie le nombre de recherches réussies."""
    locs = locations(cfg)
    market = MarketPrice(client)
    rent = cfg["rentabilite"]
    ok = 0
    for model in cfg["modeles"]:
        max_price = int(model["achat_max"] * (1 + rent["marge_negociation_achat_pct"] / 100))
        if not rent.get("respecter_grille", True):
            max_price = int(model["prix_moyen"][1])
        log.info("Recherche : %s (jusqu'à %s)", model["nom"], eur(max_price))
        try:
            result = client.search(
                category=lbc.Category.VEHICULES_VOITURES, sort=lbc.Sort.NEWEST,
                locations=locs, limit=35, price=[300, max_price], **model_filters(model),
            )
        except Exception as e:
            log.error("Échec de la recherche (%s)", e)
            continue
        ok += 1
        new_ads = [ad for ad in result.ads if str(ad.id) not in seen]
        log.info("  %d annonces, %d nouvelles", len(result.ads), len(new_ads))
        for ad in new_ads:
            try:
                a = analyse(ad, model, market, cfg)
            except Exception as e:
                # Pas marquée comme vue : elle sera réessayée au prochain passage
                log.warning("  erreur d'analyse sur %s : %s", ad.url, e)
                continue
            seen[str(ad.id)] = datetime.now().isoformat()
            if a:
                log.info("  %s %+.0f € : %s", a["label"], a["profit"], ad.subject)
                tg.send(format_message(ad, a, model, cfg),
                        ad.images[0] if ad.images else None)
            save_seen(seen)
        time.sleep(3)
    return ok


def market_report(client: lbc.Client, tg: Telegram, cfg: dict) -> None:
    """Prix du marché actuel de chaque modèle comparé à ta grille."""
    rent = cfg["rentabilite"]
    market = MarketPrice(client)
    lines = ["<b>📊 État du marché (toute la France)</b>", ""]
    for model in cfg["modeles"]:
        prices = sorted(market.for_model(model))
        if len(prices) < 5:
            lines.append(f"<b>{html.escape(model['nom'])}</b> : pas assez d'annonces ({len(prices)})\n")
            continue
        med = statistics.median(prices)
        low = prices[len(prices) // 4]
        max_buy = med * (1 - rent["negociation_revente_pct"] / 100) - rent["frais_fixes"] - rent["marge_min"]
        g_low, g_high = model["prix_moyen"]
        verdict = "✔ conforme" if g_low * 0.9 <= med <= g_high * 1.1 else (
            "⬆️ marché plus haut" if med > g_high else "⬇️ marché plus bas")
        lines.append(
            f"<b>{html.escape(model['nom'])}</b> ({len(prices)} annonces)\n"
            f"  médiane {eur(med)} · 25 % les moins chères ≤ {eur(low)}\n"
            f"  ta grille {eur(g_low)}–{eur(g_high)} → {verdict}\n"
            f"  achat max recalculé {eur(max_buy)} (ta grille : {eur(model['achat_max'])})\n"
        )
    tg.send("\n".join(lines))


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
    # Sur GitHub Actions, le token et le chat_id viennent des secrets
    token = os.environ.get("TELEGRAM_TOKEN") or cfg["telegram"]["token"]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg["telegram"]["chat_id"]
    if args.chat_id:
        print_chat_id(token)
        return
    tg = Telegram(token, str(chat_id), dry_run=args.dry_run)
    if args.test:
        tg.send("✅ Le bot Leboncoin est bien connecté !")
        return

    client = lbc.Client()
    if args.marche:
        market_report(client, tg, cfg)
        return

    seen = load_seen()
    while True:
        ok = 0
        try:
            ok = run_once(client, tg, cfg, seen)
        except Exception as e:
            log.exception("Erreur inattendue : %s", e)
            client = lbc.Client()
        if args.once:
            if ok == 0:
                sys.exit("Aucune recherche n'a abouti (Leboncoin bloque peut-être cette connexion)")
            break
        minutes = cfg.get("intervalle_minutes", 10)
        log.info("Prochain passage dans %d min", minutes)
        time.sleep(minutes * 60)


if __name__ == "__main__":
    main()
