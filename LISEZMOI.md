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
Sur un **VPS** : toutes les 5 min, 24h/24, comme service `bot-consoles` (démarre tout seul, redémarre après une coupure).
Leboncoin bloque les serveurs, donc le bot passe par un **proxy résidentiel**.
Le token Telegram, le chat_id et le proxy sont dans `~/bot/bot.env` sur le serveur, jamais dans ce dépôt.
Le lancement automatique sur GitHub est désactivé : ce dépôt sert seulement de sauvegarde du code.
Ne lance pas le bot ailleurs en même temps, sinon tu recevras tout en double.

Sur le serveur :
- `systemctl status bot-consoles` : le bot tourne-t-il ?
- `tail -f ~/bot/bot.log` : voir ce qu'il fait en direct (Ctrl+C pour quitter)
- `sudo systemctl restart bot-consoles` : le redémarrer

## Modifier la grille
Dans `config.yaml`, section `produits:` :
- `achat: [bas, haut]` : ta fourchette d'achat.
- `revente: [bas, haut]` : ta fourchette de revente.
- `prix_min` : en dessous, ce n'est jamais une vraie console (accessoires, arnaques).
- `actif: false` : met une console en pause.

Après une modification, copie `config.yaml` sur le serveur puis redémarre le bot.

## Commandes (sur ton PC)
| Commande | Effet |
|---|---|
| `python bot.py --marche --dry-run` | Prix du marché affiché dans la console |
| `python bot.py --once --dry-run` | Un passage d'essai sans Telegram |
| `python bot.py --test` | Message de test sur Telegram |

## Sécurité
- Paie **uniquement via le paiement sécurisé Leboncoin** (livraison). Jamais de virement ni de paiement en dehors du site.
- « Prix anormalement bas » : méfiance, c'est souvent une arnaque.
