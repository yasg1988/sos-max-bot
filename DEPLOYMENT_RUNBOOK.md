# Araltysh Deployment Runbook

This file exists to prevent mixing Araltysh with the unrelated school project.

## Do Not Confuse Projects

Araltysh:

- Local folder: `D:\Тревожная кнопка\sos-max-bot`
- GitHub: `yasg1988/sos-max-bot`
- GitVerse mirror: `https://gitverse.ru/yasg1988/araltysh`
- Domain: `https://sos.yasg.ru`
- VPS: `185.23.34.142`
- SSH key: `%USERPROFILE%\.ssh\claude_dokploy_key`
- Docker service: `sos-max-bot-lwl4ry`

School project:

- Local folder: `D:\medv_raion\school_4`
- Separate project, separate deploy target.
- Do not touch it while working on Araltysh.

## Secrets Policy

Do not put secrets in git.

Secrets and credentials are expected to live outside the repository:

- MAX bot token and database settings: Dokploy/service environment variables.
- Docker Hub credentials: GitHub repository secrets.
- GitVerse mirror token: GitHub repository secret `GITVERSE_TOKEN`.
- npm token: user-provided token or local npm auth, used only through temporary config.
- SSH deploy key: `%USERPROFILE%\.ssh\claude_dokploy_key`.

Before every commit or release, run:

```powershell
rg -n "dckr_pat|npm_[A-Za-z0-9]|_authToken|NPM_TOKEN|NODE_AUTH_TOKEN|MAX_BOT_TOKEN=.*|BOT_TOKEN=.*" -S .
```

Expected result: no raw secret values. Mentions of variable names such as `MAX_BOT_TOKEN` in docs/code are acceptable.

## Local Verification

```powershell
cd "D:\Тревожная кнопка\sos-max-bot"
python -m py_compile app.py
git status --short
```

## Release Version

Update `package.json`:

```json
"version": "X.Y.Z"
```

If the README Docker Hub badge contains a version-specific URL, update it to `X.Y.Z` too.

Commit and tag:

```powershell
git add app.py README.md package.json
git commit -m "fix(bot): short change description"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

Adjust `git add` files to match the actual change.

## npm Publish

Package name: `araltysh`.

Use a temporary npm config. Never write `.npmrc` to the repo.

```powershell
$token = $env:NPM_TOKEN
$tmp = Join-Path $env:TEMP ('npmrc-araltysh-' + [guid]::NewGuid().ToString())
"//registry.npmjs.org/:_authToken=$token" | Set-Content -LiteralPath $tmp -Encoding ASCII
try {
  npm publish --access public --userconfig $tmp
} finally {
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}
```

Verify:

```powershell
npm view araltysh version gitHead license --json
```

## Docker Images

GitHub Actions workflow `.github/workflows/docker-publish.yml` publishes images to:

- `ghcr.io/yasg1988/sos-max-bot:X.Y.Z`
- `ghcr.io/yasg1988/sos-max-bot:vX.Y.Z`
- `docker.io/lmserg/araltysh:X.Y.Z`
- `docker.io/lmserg/araltysh:vX.Y.Z`
- `latest`

Check workflow:

```powershell
gh run list --workflow docker-publish.yml --limit 5
gh run watch <run-id> --exit-status
```

Check Docker Hub tag:

```powershell
Invoke-RestMethod -Uri "https://hub.docker.com/v2/repositories/lmserg/araltysh/tags/X.Y.Z" | ConvertTo-Json -Depth 3
```

## GitVerse Mirror

GitVerse repository:

```text
https://gitverse.ru/yasg1988/araltysh
```

The GitHub workflow `.github/workflows/gitverse-mirror.yml` mirrors branches and tags to GitVerse on push.

Required GitHub secret:

```text
GITVERSE_TOKEN
```

If `GITVERSE_TOKEN` is absent, the workflow skips mirroring without failing.

Verify mirror state:

```powershell
git ls-remote https://gitverse.ru/yasg1988/araltysh.git HEAD refs/heads/main refs/tags/vX.Y.Z
```

## Production Deploy

Deploy only to Araltysh VPS `185.23.34.142`.

```powershell
ssh -i $env:USERPROFILE\.ssh\claude_dokploy_key -o StrictHostKeyChecking=no root@185.23.34.142 "docker pull ghcr.io/yasg1988/sos-max-bot:X.Y.Z && docker service update --image ghcr.io/yasg1988/sos-max-bot:X.Y.Z sos-max-bot-lwl4ry"
```

Verify the service:

```powershell
ssh -i $env:USERPROFILE\.ssh\claude_dokploy_key -o StrictHostKeyChecking=no root@185.23.34.142 "docker service ls --format '{{.Name}} {{.Image}} {{.Replicas}}' | grep sos-max-bot-lwl4ry && docker service ps sos-max-bot-lwl4ry --no-trunc --format '{{.CurrentState}} {{.Image}} {{.Error}}' | head -5"
```

Verify the app:

```powershell
(Invoke-WebRequest -UseBasicParsing https://sos.yasg.ru/health -TimeoutSec 20).Content
```

Expected health response contains:

- `"status":"ok"`
- `"service":"sos-max-bot"`
- `"webhook_url":"https://sos.yasg.ru/webhook/max"`

## GitHub Release

```powershell
$notes = @'
## Что изменилось

- Краткое описание изменения.

## Публикация

- npm: araltysh@X.Y.Z
- Docker Hub: lmserg/araltysh:X.Y.Z
- GHCR: ghcr.io/yasg1988/sos-max-bot:X.Y.Z
'@
$tmp = Join-Path $env:TEMP ('release-vX.Y.Z-' + [guid]::NewGuid().ToString() + '.md')
Set-Content -LiteralPath $tmp -Value $notes -Encoding UTF8
try {
  gh release create vX.Y.Z --title "Аралтыш vX.Y.Z" --notes-file $tmp --latest
} finally {
  Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}
```

Verify:

```powershell
gh release view vX.Y.Z --json tagName,name,isDraft,isPrerelease,url,publishedAt,targetCommitish
```

## Database Caution

Do not clear production tables or family links without explicit confirmation from the user.

If database cleanup is requested, make a backup first and keep it in `db_backups/` or another explicit backup path.
