# Bot achat-revente consoles Leboncoin → Telegram

## Ce que fait le bot
Toutes les 10 min, pour chaque console de ta grille (`produits:` dans `config.yaml`) :
1. Il cherche les **annonces du jour avec livraison** (Mondial Relay, Colissimo…), dans toute la France.
2. Il écarte :
   - les annonces **main propre uniquement** ;
   - les consoles **en panne**, pour pièces, à l'écran cassé ou **piratées** ;
   - les **accessoires** rangés en « console » (manettes, jeux, façades…) ;
   - les annonces « je cherche ».
3. Il calcule le **prix du marché en direct** (médiane des consoles livrables en état de marche) et le compare à ta fourchette de revente.
4. Il calcule ton **bénéfice** : revente − 5 % de négo − prix d'achat − frais d'achat (port + protection Leboncoin) − réparations repérées (drift, sans dock…).
5. Il t'envoie sur Telegram :
   - 🔥🔥 **AFFAIRE EN OR** : prix sous le bas de ta fourchette d'achat ;
   - 🔥 **BONNE AFFAIRE** : sous ton achat max ;
   - 💬 **À NÉGOCIER** : un peu cher, avec le prix à proposer.

Chaque matin à 8h, il envoie aussi le **prix du marché** de chaque console.

## Où il tourne
Sur **GitHub Actions** (gratuit, dépôt public) : toutes les 10 min, 24h/24.
Leboncoin bloque les serveurs de GitHub, donc le bot passe par un **proxy résidentiel DataImpulse**.
Le token Telegram, le chat_id et le proxy sont dans les **Secrets** du dépôt (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `PROXY_URL`), invisibles même en public.
Suivi des passages : onglet **Actions** du dépôt.
Ne lance pas le bot sur ton PC en même temps, sinon tu recevras tout en double.

## Modifier la grille
Dans `config.yaml`, section `produits:` :
- `achat: [bas, haut]` : ta fourchette d'achat.
- `revente: [bas, haut]` : ta fourchette de revente.
- `prix_min` : en dessous, ce n'est jamais une vraie console (accessoires, arnaques).
- `actif: false` : met une console en pause.

Après une modification, envoie-la sur GitHub : le passage suivant l'utilise.

## Commandes (sur ton PC)
| Commande | Effet |
|---|---|
| `python bot.py --marche --dry-run` | Prix du marché affiché dans la console |
| `python bot.py --once --dry-run` | Un passage d'essai sans Telegram |
| `python bot.py --test` | Message de test sur Telegram |

## Sécurité
- Paie **uniquement via le paiement sécurisé Leboncoin** (livraison). Jamais de virement ni de paiement en dehors du site.
- « Prix anormalement bas » : méfiance, c'est souvent une arnaque.
