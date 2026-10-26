"""
Version hébergée du bot (Render, offre gratuite).

Render ne garde gratuitement qu'un "web service" : ce petit serveur web
répond aux visites, et le bot tourne en arrière-plan dans le même programme.

Pages :
    /         état du bot (dernier passage, recherches réussies)
    /test     fait une recherche Leboncoin tout de suite : dit si l'hébergeur est bloqué
"""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lbc
from curl_cffi import requests

import bot

status = {"last_pass": None, "searches_ok": None, "searches_total": None}


def test_leboncoin() -> str:
    try:
        result = bot.make_client().search(category=lbc.Category.VEHICULES_VOITURES, limit=5)
        return f"OK : Leboncoin répond ({len(result.ads)} annonces reçues)"
    except Exception as e:
        return f"BLOQUÉ : {e}"


def keep_awake() -> None:
    """Render endort le service après 15 min sans visite : on se visite soi-même."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        time.sleep(600)
        try:
            requests.get(url, timeout=30)
        except Exception as e:
            bot.log.warning("Réveil impossible : %s", e)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/test"):
            body = test_leboncoin()
        else:
            if status["last_pass"]:
                body = (f"Bot actif. Dernier passage : {status['last_pass']} "
                        f"({status['searches_ok']}/{status['searches_total']} recherches réussies)")
            else:
                body = "Bot démarré, premier passage en cours..."
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main() -> None:
    cfg = bot.load_config()
    tg = bot.telegram_from_config(cfg)
    threading.Thread(target=bot.run_loop, args=(tg, cfg), kwargs={"status": status},
                     daemon=True).start()
    threading.Thread(target=keep_awake, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    bot.log.info("Serveur sur le port %d", port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
