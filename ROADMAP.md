# Capturio — Roadmap des améliorations futures

## 1. Backup / Restore

### Objectif
Permettre à l'administrateur d'exporter l'intégralité des données et de les restaurer sur une autre instance (ou après une perte de données).

### Ce que ça couvre
- Métadonnées DB : photos, albums, spaces, posts, associations (album_photos)
- Thumbnails (optionnel — régénérables, donc secondaires)
- Fichiers photos originaux (hors scope : trop volumineux, restent sur le volume `/photos`)

### Fonctionnement envisagé

#### Export (Backup)
- Bouton **"Export backup"** sur le dashboard admin
- Génère un fichier `.zip` en arrière-plan contenant :
  - `backup.json` — dump de toutes les tables en JSON (photos, albums, spaces, posts, album_photos)
  - `meta.json` — version Capturio, date, stats (nb photos, albums, etc.)
- Streaming du zip via `StreamingResponse` (pas de stockage temporaire côté serveur)
- Pas de dépendance à `pg_dump` — export pur SQLAlchemy pour portabilité

#### Import (Restore)
- Page `/admin/backup` avec upload d'un `.zip` ou `.json`
- Deux modes :
  - **Merge** — importe uniquement les enregistrements absents (par `id` UUID), ne touche pas l'existant
  - **Full restore** — vide les tables puis réimporte (avec confirmation modale)
- Validation du fichier avant import (version, structure)
- Résumé après restore : X photos restaurées, Y skippées (déjà présentes), Z erreurs

### Fichiers à créer / modifier
- `app/routers/admin.py` — routes `GET /admin/backup`, `POST /admin/backup/export`, `POST /admin/backup/restore`
- `app/templates/admin/backup.html` — page avec deux sections (export + import)
- `app/services/backup.py` — logique de sérialisation/désérialisation JSON + zip
- `app/static/css/style.css` — styles backup page (mineurs)
- `app/templates/admin/base_admin.html` — lien "Backup" dans sidebar

---

## 2. Review d'import — Explorateur de fichiers avancé

### Objectif
Remplacer/compléter la page "Import from folders" actuelle avec une vue détaillée permettant de voir l'état exact de chaque fichier dans l'arborescence, filtrer, et agir sur des sélections.

### Problèmes actuels
- On ne voit les fichiers d'un dossier qu'en cliquant sur le triangle (expand), un dossier à la fois
- Impossible de voir d'un coup tous les fichiers non importés sur l'ensemble des dossiers
- Pas de possibilité de sélectionner des fichiers individuellement à importer

### Fonctionnement envisagé

#### Vue globale `/admin/import-review`
- Tableau de tous les fichiers image trouvés récursivement dans `PHOTOS_DIR`
- Colonnes : chemin relatif, taille, date fichier, statut (✓ Importé / — Non importé)
- Filtres en haut : **Tous / Non importés / Importés**, + champ recherche par nom
- Pagination (100 par page) pour éviter de charger 10 000 lignes
- Compteurs dans le header : "326 fichiers — 289 importés — 37 restants"

#### Sélection et import ciblé
- Checkbox par ligne (même pattern que la page Photos : click row, shift+click range)
- Bouton **"Importer la sélection"** → lance un job background pour les fichiers sélectionnés uniquement
- Bouton **"Importer tous les non-importés"** → job background global

#### Détail d'un fichier (modal ou expand)
- Click sur un fichier → affiche : preview thumbnail (si importé) ou icône fichier, EXIF extrait à la volée, chemin complet, taille, date
- Si déjà importé : lien "Voir dans Photos" → `/admin/photos/{id}/edit`

#### Job progress
- Réutilise le banner de polling existant (`/admin/import-jobs`)
- Même système que l'import par dossier

### Fichiers à créer / modifier
- `app/routers/admin.py` — routes `GET /admin/import-review`, `GET /admin/api/all-files`, `POST /admin/import-selection`
- `app/services/scanner.py` — nouvelle fonction `list_all_files(photos_dir, db)` retournant statut importé par fichier
- `app/templates/admin/import_review.html` — nouvelle page
- `app/static/css/style.css` — styles review (réutilise `.bulk-action-bar`, `.photo-row`)
- `app/templates/admin/base_admin.html` — lien "Import Review" dans sidebar (ou sous "Import")

---

## Priorité suggérée

| # | Feature | Complexité | Valeur |
|---|---------|-----------|--------|
| 1 | Import Review | Moyenne | Haute — indispensable pour gérer de grandes bibliothèques |
| 2 | Backup/Export JSON | Faible | Haute — sécurité des données |
| 3 | Restore (merge mode) | Moyenne | Moyenne |
| 4 | Restore (full wipe) | Faible | Basse — rare, risqué |
