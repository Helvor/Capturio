# Capturio

Self-hosted photo gallery for photographers. Built with FastAPI + PostgreSQL, fully containerized with Docker.

---

## Features

- Public gallery with album filtering and pagination
- Per-photo EXIF panel (camera, lens, aperture, shutter, ISO, dimensions)
- Download button (optional per photo)
- Album management with custom photo ordering
- Posts & pages with Markdown support
- Pinned announcements
- Admin interface protected by JWT (HttpOnly cookie)
- Auto-scan of a local folder — no manual imports needed
- Drag & drop upload in the admin
- Thumbnails auto-generated as WEBP at 800px

---

## Quick start

### 1. Clone and configure

```bash
git clone <repo-url>
cd capturio
cp .env.example .env
```

Edit `.env` and fill in all values:

```
POSTGRES_USER=capturio
POSTGRES_PASSWORD=a_strong_password
POSTGRES_DB=capturio
SECRET_KEY=a_64_char_random_string
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=       # see step 2
PHOTOS_DIR=/photos
CACHE_DIR=/app/cache
APP_PORT=8000              # change to any free port on your host
```

### 2. Generate the SECRET_KEY

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output (64 hex characters) into `SECRET_KEY` in your `.env`.

### 3. Generate the admin password hash

```bash
python3 scripts/generate_password_hash.py yourpassword
```

Or via Docker if you don't have Python locally:

```bash
docker run --rm python:3.12-slim sh -c \
  "pip install bcrypt -q && python3 -c \
  \"import bcrypt, sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())\" yourpassword"
```

Copy the output (`$2b$12$...`) into `ADMIN_PASSWORD_HASH` in your `.env`.

### 4. Create local folders

```bash
mkdir -p photos cache
```

### 5. Start

```bash
docker compose up -d
```

The app runs on `http://localhost:${APP_PORT}` (default 8000).

Database migrations run automatically on startup.

### 6. First use

1. Open `http://localhost:8000/admin` → sign in
2. Drop some JPEG / PNG / WEBP files into the `./photos/` folder
3. Click **Scan photos folder** → photos are imported with EXIF & thumbnails
4. Edit each photo to set title / description, then publish
5. Create albums, add photos, publish
6. Write an **about** page post with slug `about`

---

## Changing the port

Edit `APP_PORT` in `.env` and restart:

```bash
docker compose down && docker compose up -d
```

---

## Folder structure

```
photos/          ← bind-mounted to /photos inside the container
  uploads/       ← manual uploads land here
cache/
  thumbs/        ← auto-generated WEBP thumbnails
```

---

## Reverse proxy (HTTPS)

The container exposes a plain HTTP port. Use Nginx, Caddy, or Traefik on the host to handle TLS termination.

Example Caddy config:

```
yourdomain.com {
    reverse_proxy localhost:8000
}
```

---

## Environment variables reference

| Variable | Description |
|---|---|
| `POSTGRES_USER` | PostgreSQL username |
| `POSTGRES_PASSWORD` | PostgreSQL password |
| `POSTGRES_DB` | Database name |
| `SECRET_KEY` | JWT signing key (min 32 chars, keep secret) |
| `ADMIN_USERNAME` | Admin login username |
| `ADMIN_PASSWORD_HASH` | bcrypt hash of admin password |
| `PHOTOS_DIR` | Mount path for original photos (default `/photos`) |
| `CACHE_DIR` | Mount path for thumbnails (default `/app/cache`) |
| `APP_PORT` | Host port to bind (default `8000`) |
