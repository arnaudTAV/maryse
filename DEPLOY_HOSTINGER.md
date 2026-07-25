# Déploiement sur VPS Hostinger

Guide pas-à-pas pour installer `prepress-mcp` sur un VPS Hostinger déjà créé,
sans nom de domaine pour l'instant (accès par IP nue, HTTP simple — le TLS
s'ajoute en une ligne le jour où un domaine est prêt).

Deux services tournent via Docker Compose : l'application (jamais exposée
directement à internet) et Caddy devant elle (seul point d'entrée public,
port 80/443).

## 0. Ce qu'il te faut avant de commencer

- L'IP publique du VPS et l'accès SSH (mot de passe ou clé, fournis par Hostinger).
- Le VPS tourne sous Ubuntu (22.04 ou plus récent) — c'est l'image par défaut
  proposée par Hostinger pour les VPS ; si tu as choisi une autre distribution,
  adapte les commandes `apt` ci-dessous.

## 1. Se connecter au VPS

```bash
ssh root@<IP_DU_VPS>
```

## 2. Installer Docker

```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
# Vérifie que Docker Compose (plugin) est bien présent :
docker compose version
```

## 3. Ouvrir le pare-feu

```bash
apt install -y ufw
ufw allow 22/tcp     # SSH — à faire AVANT d'activer ufw pour ne pas te bloquer dehors
ufw allow 80/tcp      # HTTP (Caddy)
ufw allow 443/tcp     # HTTPS (Caddy, une fois un domaine configuré)
ufw enable
```

## 4. Envoyer le projet sur le VPS

Depuis ta machine, en remplaçant `<IP_DU_VPS>` :

```bash
scp -r prepress-mcp root@<IP_DU_VPS>:/opt/prepress-mcp
```

(Ou `git clone` si le projet est poussé sur un dépôt Git accessible depuis le VPS.)

## 5. Créer le fichier des tenants (tokens d'authentification)

Sur le VPS :

```bash
cd /opt/prepress-mcp
cp tenants.toml.example tenants.toml

# Génère un vrai token aléatoire pour chaque client de l'API :
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Édite `tenants.toml` et remplace les `REPLACE_WITH_RANDOM_TOKEN_*` par les
tokens générés. Garde ces valeurs précieusement — c'est ce que les
applications appelantes devront envoyer dans l'en-tête
`Authorization: Bearer <token>`.

## 6. Démarrer les services

```bash
docker compose up -d --build
docker compose ps        # les deux conteneurs doivent être "running"
docker compose logs -f   # Ctrl+C pour sortir des logs
```

`docker-compose.yml` monte `Caddyfile.ip` par défaut — Caddy écoute en HTTP
simple sur le port 80 de l'IP du VPS, sans certificat (normal, une IP nue ne
peut pas avoir de certificat Let's Encrypt).

## 7. Vérifier que ça répond

```bash
curl http://<IP_DU_VPS>/healthz
# -> ok
```

## 8. Tester l'endpoint REST classique

Upload direct d'un PDF :

```bash
curl -X POST http://<IP_DU_VPS>/api/v1/lowres-report \
  -H "Authorization: Bearer <TON_TOKEN>" \
  -F "file=@/chemin/vers/fichier.pdf" \
  -F "target_width_mm=148" \
  -F "target_height_mm=210" \
  -F "dpi_threshold=250"
```

Ou avec une URL signée plutôt qu'un upload :

```bash
curl -X POST http://<IP_DU_VPS>/api/v1/lowres-report \
  -H "Authorization: Bearer <TON_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"source_pdf_url": "https://exemple.com/fichier.pdf", "target_width_mm": 148, "target_height_mm": 210, "dpi_threshold": 250}'
```

Réponse : un JSON avec `status`, la liste `flagged`, et soit
`html_report_path` (chemin sur le serveur, tant qu'il n'y a pas de domaine),
soit `html_report_url` une fois `PREPRESS_PUBLIC_BASE_URL` renseigné (étape 10).

## 9. Le tool MCP reste disponible en parallèle

Même serveur, même port : `http://<IP_DU_VPS>/mcp` expose
`preflight_pdf_images_res` et `preflight_pdf_images_res_at_target_size` pour
tout client MCP (Claude compris), avec le même token bearer. Le endpoint REST
de l'étape 8 ne remplace rien, il s'ajoute.

## 10. Quand un nom de domaine est prêt

1. Pointe un enregistrement DNS A du domaine vers l'IP du VPS.
2. Sur le VPS, édite `docker-compose.yml` : remplace
   `./Caddyfile.ip:/etc/caddy/Caddyfile:ro` par `./Caddyfile:/etc/caddy/Caddyfile:ro`.
3. Édite `Caddyfile` : remplace `prepress.example.com` par ton vrai domaine.
4. Édite `docker-compose.yml`, service `prepress-mcp` : mets
   `PREPRESS_PUBLIC_BASE_URL: "https://ton-domaine.com"`.
5. `docker compose up -d` — Caddy obtient un certificat Let's Encrypt
   automatiquement, aucune autre étape.

## Maintenance

```bash
docker compose logs -f prepress-mcp   # logs applicatifs
docker compose restart prepress-mcp   # redémarrer après une modif de tenants.toml
docker compose down && docker compose up -d --build   # après une mise à jour du code
```

Les rapports générés (PDF annotés, rapports HTML) vivent dans le volume Docker
`prepress_reports`, isolés par tenant sous `/srv/prepress/tenants/<tenant_id>/reports/`.
Ils survivent aux redémarrages et mises à jour du conteneur.
