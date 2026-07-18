# Contributing

## Branch model

- **`main`** — stable/production. Protected: changes land only via pull request
  with passing CI (Backend + Frontend checks). Force-pushes and deletion are
  blocked.
- **`dev`** — integration branch. Day-to-day work happens here.

## Workflow

```bash
# work on dev
git switch dev
git pull

# ...make changes, commit (signed)...
git commit -S -m "your change"
git push

# open a PR into main and let CI gate the merge
gh pr create --base main --head dev --title "…" --body "…"
# when the Backend + Frontend checks are green:
gh pr merge --merge
```

## Local checks (mirror CI)

```bash
# backend
pip install -r requirements-dev.txt
ruff check server tests
pytest -q

# frontend
cd frontend && npm ci && npm run lint && npm run build
```

## Notes

- CI runs on pushes to `main`/`dev` and on PRs (`.github/workflows/ci.yml`),
  plus CodeQL security analysis.
- Commits are GPG-signed. If signing fails with "Inappropriate ioctl for
  device", run `export GPG_TTY=$(tty)` first.
- Keep PRs focused; the demo (`docker compose -f docker-compose.demo.yml up`)
  is handy for eyeballing UI changes with synthetic data.
