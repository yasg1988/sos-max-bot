# SOS MAX Bot

Production MAX messenger bot for family SOS alerts and organization alarm workflows.

## Runtime

- FastAPI webhook service
- PostgreSQL schemas: `sos_core`, `sos_family`, `sos_org`
- MAX Bot API

## Required Env

- `MAX_BOT_TOKEN`
- `MAX_WEBHOOK_URL`
- `ADMIN_USER_IDS`
- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSLMODE`

Secrets must be configured in Dokploy, not committed to the repository.
