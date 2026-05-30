# Codex Instructions For This Project

This repository is the Araltysh / `sos-max-bot` project. It is not the school project.

## Hard Boundaries

- Work only in `D:\Тревожная кнопка\sos-max-bot` for this bot.
- Do not touch, deploy, commit, or "fix" `D:\medv_raion\school_4` unless the user explicitly asks to work on the school project.
- Do not deploy Araltysh to the school Dokploy instance. The school project is separate and must stay untouched.
- Never commit raw tokens, passwords, `.env` values, npm tokens, Docker tokens, MAX bot tokens, or database credentials.

## Correct Project

- Local repo: `D:\Тревожная кнопка\sos-max-bot`
- GitHub repo: `https://github.com/yasg1988/sos-max-bot`
- GitVerse mirror: `https://gitverse.ru/yasg1988/araltysh`
- Public package: `araltysh`
- Production domain: `https://sos.yasg.ru`
- Health URL: `https://sos.yasg.ru/health`
- Production VPS for Araltysh: `185.23.34.142`
- SSH key for Araltysh VPS: `%USERPROFILE%\.ssh\claude_dokploy_key`
- Docker Swarm service: `sos-max-bot-lwl4ry`
- Production image should be updated from GHCR: `ghcr.io/yasg1988/sos-max-bot:<version>`
- Docker Hub mirror: `lmserg/araltysh:<version>`

## Standard Release Flow

1. Make the scoped code/docs change.
2. Run `python -m py_compile app.py`.
3. Search for leaked secrets before commit:
   `rg -n "dckr_pat|npm_[A-Za-z0-9]|_authToken|NPM_TOKEN|NODE_AUTH_TOKEN|MAX_BOT_TOKEN=.*|BOT_TOKEN=.*" -S .`
4. Bump `package.json` version.
5. If README has version-specific Docker Hub badge URL, update it to the same version.
6. Commit to `main`, tag `vX.Y.Z`, push branch and tag.
7. Publish npm package only through a temporary npm config or an environment secret. Do not save token files in the repo.
8. Wait for GitHub Actions Docker publish workflow to succeed.
9. Check GitVerse mirror workflow. It uses GitHub Secret `GITVERSE_TOKEN`; if the secret is absent, the workflow skips without failing.
10. Deploy the new GHCR image to the Araltysh service on `185.23.34.142`.
11. Verify `https://sos.yasg.ru/health`.
12. Create a GitHub Release for the tag.

See `DEPLOYMENT_RUNBOOK.md` for exact commands.
