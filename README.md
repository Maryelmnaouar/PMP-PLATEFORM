# AssetPilot — Maintenance Autonome

Plateforme web de **Maintenance Autonome (AM / TPM)** :
- TAGs AM / RCM / Safety et tableau de suivi des anomalies ;
- checklists préventives ;
- tableau SGA digital et audits AM sur 3 niveaux ;
- gestion PDR ;
- intégration **SAP S/4HANA** (avis de maintenance).

Technologies : Python 3.13 · Flask · SQLite · utilisable sur tablette (installable sur l'écran d'accueil).

---

## 1. Démarrage en local

```bash
python -m venv venv
# Windows : .\venv\Scripts\Activate.ps1      macOS / Linux : source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Windows : copy .env.example .env — puis adapter (facultatif en local)
python app.py
```

Ouvrir `http://127.0.0.1:5000`.

**Premier démarrage :**
- la base est créée automatiquement avec :
  - le compte **administrateur** ;
  - l'usine **Cobomi**, la ligne **MOR_CA_CSD_PET4** et ses **26 équipements** (références SAP).
- Aucune donnée d'exemple n'est créée : ni utilisateurs, ni TAGs, ni checklists.
- Si `ADMIN_PASSWORD` n'est pas défini, un **mot de passe provisoire** s'affiche dans la console (ou dans les logs Render). Changez-le ensuite dans **Mon compte**.

## 2. Variables d'environnement

| Variable | Rôle | Valeur conseillée |
|---|---|---|
| `SECRET_KEY` | Clé de chiffrement des sessions | Longue chaîne aléatoire (générée automatiquement par Render) |
| `DATA_DIR` | Dossier de la base SQLite et des photos | Render : `/var/data` · local : vide (dossier du projet) |
| `ADMIN_USERNAME` | Identifiant de l'administrateur créé au 1er démarrage | ex. `melmnaouar` |
| `ADMIN_PASSWORD` | Son mot de passe initial | Vide = mot de passe provisoire affiché dans les logs |
| `ADMIN_NAME` | Nom affiché de l'administrateur | ex. `Maryam Elmnaouar` |
| `SESSION_COOKIE_SECURE` | Cookie de session uniquement en HTTPS | `1` en production |
| `SEED_BASE_DATA` | Créer Cobomi, la ligne et les 26 équipements au 1er démarrage | `1` (ou `0` pour une base vide) |

Les variables `ADMIN_*` et `SEED_BASE_DATA` ne servent qu'à la **création** de la base. Elles sont ignorées ensuite.

## 3. Mise en place après le premier démarrage (administrateur)
1. **Paramètres → SGA** : créer les SGA et cocher les équipements de chacune.
2. **Utilisateurs & accès** : créer les comptes, avec leur type, leurs SGA, leurs droits et, pour les chefs d'équipe, leur **utilisateur SAP**.
3. **Checklists préventives** : créer les checklists et les affecter aux opérateurs et techniciens.
4. **SOP / Documentation** : ajouter les liens Google Drive des SOP, OPL et fiches techniques.
5. **Intégration SAP** : choisir le mode (désactivé par défaut) et saisir l'adresse du Fiori de l'entreprise. Détails dans `docs/GUIDE_INTEGRATION_SAP.md`.

---

## 3 bis. Fonctionnement (V6.2)
- **Première connexion** : chaque compte créé par l'administrateur, ainsi que l'administrateur initial, doit choisir un mot de passe personnel avant d'accéder à la plateforme. Un mot de passe réinitialisé par l'administrateur l'impose à nouveau.
- **TAGs** :
  - « Anomalie découverte par » se choisit dans la liste des utilisateurs.
  - Les TAGs AM n'ont pas de N° d'OT.
  - Les TAGs **RCM** et **Safety** restent en « Anomalies découvertes » tant que le **chef d'équipe production** ne les a pas validés. Il crée l'avis dans SAP avec les informations à copier, puis saisit le **N° d'avis**, qui est obligatoire.
  - Pour passer en « Traité », la **personne qui a traité** est obligatoire, ainsi que le **N° d'OT** pour les TAGs RCM et Safety.
  - L'administrateur peut **modifier** ou **supprimer** un TAG.
- **Contre-mesures** : elles sont enregistrées dans la page **Contre-mesures** et ne sont plus ajoutées automatiquement aux checklists. Depuis cette page, l'administrateur les **intègre** dans une checklist CILT existante ou nouvelle.
- **Checklists CILT** : une checklist correspond à une machine et contient des tâches. Chaque tâche a un organe, un type (Cleaning / Inspection / Lubrification / Serrage), un outillage, un temps et une fréquence (quotidienne → annuelle).
- **Distribution des tâches** : les tâches ne sont plus générées automatiquement. L'administrateur les **distribue** par semaine (fréquences quotidienne et hebdomadaire), par mois (mensuelle et trimestrielle) ou par année (semestrielle et annuelle), en choisissant la personne concernée. La plateforme retient la dernière personne choisie et la date de la dernière distribution.

## 4. Mettre le projet sur GitHub

> Créez un dépôt **privé** : la plateforme contient la structure de votre usine.
> Ni la base de données, ni les photos, ni le fichier `.env` ne sont envoyés : ils sont exclus par `.gitignore`.

1. Sur github.com : **New repository** → nom `assetpilot-am` → **Private** → ne cochez **pas** « Add a README » → **Create repository**.
2. Dans le dossier du projet (le dépôt Git est déjà initialisé, avec un premier commit) :
   ```bash
   git remote add origin https://github.com/<votre-compte>/assetpilot-am.git
   git branch -M main
   git push -u origin main
   ```
   *(Si vous partez du zip sans le dossier `.git`, faites d'abord : `git init`, `git add .`, puis `git commit -m "AssetPilot AM"`.)*

## 5. Déployer sur Render

### Ce qu'il faut savoir avant
- La base SQLite et les photos doivent être sur un **disque persistant**. Sur le plan **gratuit**, Render efface les fichiers à chaque redémarrage ou déploiement, et vous perdriez toutes les données. Le fichier `render.yaml` utilise donc le plan **0.5c-512mb (7 $/mois)** avec un **disque de 1 Go** (environ 0,25 $/mois).
- Avec un disque, l'application tourne sur **une seule instance**. Chaque déploiement provoque une courte coupure de quelques secondes. C'est adapté à l'usage d'une usine.

### Étapes (déploiement automatique avec `render.yaml`)
1. Créez un compte sur [render.com](https://render.com) et connectez votre compte **GitHub**.
2. **New → Blueprint** → choisissez le dépôt `assetpilot-am`. Render lit le fichier `render.yaml`.
3. Renseignez les valeurs demandées :
   - `ADMIN_USERNAME`, par exemple `melmnaouar` ;
   - `ADMIN_PASSWORD`, votre mot de passe initial ;
   - `ADMIN_NAME`, par exemple `Maryam Elmnaouar`.
   `SECRET_KEY` est générée automatiquement.
4. **Apply**. Render installe l'application, crée le disque `/var/data` et démarre le service.
5. Ouvrez l'adresse fournie (`https://assetpilot-am.onrender.com` ou similaire) et connectez-vous avec le compte administrateur.

### Étapes (sans Blueprint, par l'interface)
**New → Web Service** → dépôt GitHub, puis :

| Champ | Valeur |
|---|---|
| Runtime | Python |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120` |
| Instance | 0.5c-512mb (ou supérieur) |
| Health Check Path | `/healthz` |
| Disque | Advanced → **Add Disk** : mount path `/var/data`, taille 1 Go |
| Environment | Variables du § 2, avec `DATA_DIR=/var/data` et `SESSION_COOKIE_SECURE=1` |

### Après la mise en ligne
- **Mises à jour** : chaque `git push` sur `main` redéploie automatiquement. La base sur le disque est **conservée**.
- **Sauvegardes** : **Paramètres → ⬇ Sauvegarder la base** télécharge une copie complète. Faites-le régulièrement, par exemple chaque semaine.
- **Nom de domaine** (facultatif) : Render → Settings → Custom Domains, par exemple `assetpilot.votre-entreprise.com`. Le HTTPS est fourni automatiquement.
- **Tablettes** : une fois en ligne en HTTPS, on peut installer AssetPilot sur l'écran d'accueil (Safari → Partager → « Sur l'écran d'accueil », ou Chrome → « Installer l'application »).

### SAP depuis Render
- **Mode Fiori prérempli** : il fonctionne directement. C'est le navigateur du chef d'équipe, connecté au réseau de l'entreprise, qui ouvre le Fiori.
- **Mode API directe** : Render est sur Internet. La passerelle SAP doit donc être joignable de façon sécurisée (exposition HTTPS contrôlée, SAP BTP / Cloud Connector, ou hébergement interne d'AssetPilot). À voir avec l'IT : `docs/GUIDE_INTEGRATION_SAP.md`.

---

## 6. Structure

```
├── app.py                  # application Flask (routes, droits, tâches, indicateurs)
├── sap_integration.py      # intégration SAP S/4HANA (Fiori prérempli / API OAuth)
├── requirements.txt        # Flask, requests, gunicorn
├── render.yaml             # déploiement Render (service web + disque persistant)
├── .python-version         # Python 3.13
├── .env.example            # modèle de configuration
├── docs/GUIDE_INTEGRATION_SAP.md
├── static/                 # CSS, JS, icônes, manifeste de l'application installable
├── templates/              # pages HTML (Jinja)
└── uploads/                # photos des TAGs en local (sur Render : /var/data/uploads)
```

## 7. Sécurité
- Aucun mot de passe dans le code. Le compte administrateur est créé à partir des variables d'environnement.
- Les mots de passe sont stockés sous forme chiffrée (hachage). Chaque utilisateur peut changer le sien dans **Mon compte**.
- Sessions : cookies sécurisés en HTTPS, durée de 12 h.
- Gardez le dépôt GitHub **privé**, et ne versionnez jamais `.env` ni les fichiers `.db`.
