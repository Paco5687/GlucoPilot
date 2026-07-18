# Deployment

GlucoPilot is one container serving the API and the built SPA on port `8000`.
It needs HTTPS in front of it (OAuth redirect URIs require it).

## 1. Configure

```bash
cp .env.example .env
```

At minimum set a strong, stable `APP_SECRET_KEY` and your `APP_PUBLIC_URL`.
Everything else (integration credentials, AI provider) can be entered later on
the in-app **Settings** page — those are stored in the database and override env.

## 2. Put HTTPS in front

The shipped `docker-compose.yml` includes **Traefik** labels. Set in `.env`:

```
TRAEFIK_HOST=glucopilot.example.com
TRAEFIK_NETWORK=proxy          # your Traefik's external Docker network
TRAEFIK_ENTRYPOINT=websecure
TRAEFIK_CERTRESOLVER=letsencrypt
```

Using **nginx/Caddy** instead? Remove the `labels:` and `networks:` blocks,
publish the port, and reverse-proxy to it:

```yaml
    ports:
      - "127.0.0.1:8000:8000"
```

Then proxy your domain to `127.0.0.1:8000` with your TLS of choice.

## 3. Launch

```bash
docker compose up -d --build
```

Open `APP_PUBLIC_URL` and complete first-run admin setup.

## 4. Redirect URIs

When registering OAuth apps, use these exact redirect URIs (must match `.env`):

- Dexcom: `{APP_PUBLIC_URL}/dexcom/callback`
- Oura:   `{APP_PUBLIC_URL}/oura-callback`
- Fitbit: `{APP_PUBLIC_URL}/fitbit-callback`

## Operations

```bash
# Admin password reset
docker compose exec glucopilot python -m server.reset_password

# One-time imports (mount the folder first — see docker-compose.yml)
docker compose exec glucopilot python -m server.import_base44_export /import/GlucoseReading_export.csv /import/Treatment_export.csv

# Logs
docker compose logs -f glucopilot
```

## Backups

Everything is in the `glucopilot_data` Docker volume (SQLite DB + uploaded
records). Back it up:

```bash
docker run --rm -v glucopilot_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/glucopilot-backup.tar.gz -C /data .
```

## Private AI

For a setup where no health data leaves your machine, run a local model and
select it on the Settings page. See [LOCAL_MODELS.md](LOCAL_MODELS.md).
