# Bot achat-revente Leboncoin → Telegram

## Ce que fait le bot
Pour chaque modèle de ta grille (`modeles:` dans `config.yaml`), toutes les 10 min :
1. Il cherche les **nouvelles annonces à prix bas** (jusqu'à ton achat max + 12 % négociables).
2. Il écarte les annonces **mal classées** (une Twingo rangée en « Clio ») et celles avec des mots comme « HS » ou « pour pièces ».
3. Il calcule le **prix du marché en direct** : médiane des annonces du même modèle, année ±1, km proche, même carburant. Puis il le compare à ta fourchette `prix_moyen`.
4. Il calcule la **rentabilité** :
   - revente = marché (mode prudent : le plus bas entre le marché et le milieu de ta grille)
   - bénéfice = revente − 8 % de négo − frais (500 € + travaux repérés dans l'annonce) − prix d'achat
5. Il lit le **rapport d'historique Autoviza** joint par Leboncoin et écarte les annonces à sinistre, compteur trafiqué, procédure VE/VGE, trop de propriétaires ou passé de taxi.
6. Il t'envoie sur Telegram :
   - 🔥🔥 **AFFAIRE EN OR** : prix dans ton achat visé et bénéfice ≥ 500 €
   - 🔥 **BONNE AFFAIRE** : sous ton achat max
   - 💬 **À NÉGOCIER** : un peu cher, le bot te dit combien proposer

   Chaque message contient le bénéfice estimé, la rentabilité en %, le prix à proposer, les ✅ et ⚠️, et les liens.

## Installation (une seule fois)
```
pip install -r requirements.txt
```

## Créer le bot Telegram
1. Sur Telegram, ouvre **@BotFather**, tape `/newbot` et copie le **token** dans `config.yaml`.
2. Envoie un message à ton bot, puis lance `python bot.py --chat-id` et copie le numéro dans `chat_id:`.
3. Lance `python bot.py --test` pour vérifier.

## Commandes
| Commande | Effet |
|---|---|
| `lancer.bat` ou `python bot.py` | Lance le bot en continu |
| `python bot.py --marche` | Prix du marché actuel de chaque modèle comparé à ta grille (envoyé sur Telegram) |
| `python bot.py --once --dry-run` | Un passage d'essai affiché dans la console, sans Telegram |

## Hébergement gratuit 24h/24 (GitHub Actions)
Le bot tourne toutes les 30 min de 7h à 21h30 sur les serveurs de GitHub, même PC éteint. Gratuit, sans carte bancaire.

1. Crée un compte sur https://github.com, puis un **nouveau dépôt privé** (bouton « New », nom `bot-leboncoin`, case « Private »). Personne d'autre que toi ne voit le code ni ta grille. Le rythme est calibré pour rester dans les 2 000 min/mois gratuites (à vérifier dans *Settings → Billing*).
2. Laisse `COLLE_TON_TOKEN_ICI` dans config.yaml : le token va dans les secrets. Dans le dépôt, va dans *Settings → Secrets and variables → Actions → New repository secret* et crée :
   - `TELEGRAM_TOKEN` : ton token BotFather
   - `TELEGRAM_CHAT_ID` : ton chat_id
3. Envoie les fichiers du dossier dans le dépôt (*Add file → Upload files*, glisse tout le contenu, y compris le dossier `.github`).
4. Onglet **Actions** → « Bot Leboncoin » → **Run workflow** pour un premier essai.

Si l'exécution échoue (croix rouge, GitHub t'envoie un e-mail), regarde le journal. « Aucune recherche n'a abouti » veut dire que Leboncoin bloque les serveurs GitHub : dans ce cas, fais tourner le bot sur ton PC ou sur un vieux téléphone Android.

## Réglages utiles (config.yaml)
- `departements: ["13", "83"]` : ne cherche que près de chez toi (trajets moins chers). Par défaut, toute la France.
- `rentabilite:` : négo, frais, marge minimum, mode prudent, respect de ta grille.
- `frais_travaux:` : coût ajouté quand l'annonce parle d'embrayage, de voyant, etc.
- Ajouter un modèle : copie un bloc dans `modeles:`.

## Limites
- Au premier lancement, le bot envoie toutes les affaires en ligne d'un coup (souvent plusieurs dizaines). Ensuite, uniquement les nouvelles.
- Le prix du marché vient des prix *affichés*. Les ventes réelles se font souvent un peu en dessous, d'où la négo de 8 % et le mode prudent.
- Sans rapport Autoviza, demande au vendeur son **code HistoVec** (https://histovec.interieur.gouv.fr).
- « Prix anormalement bas » = méfiance : arnaque ou défaut caché. Ne verse jamais d'acompte avant d'avoir vu la voiture.
