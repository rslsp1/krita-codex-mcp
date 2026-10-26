# RSL fork maintenance

- `upstream`: https://github.com/cyyprezz/krita-codex-mcp.git
- `origin`: https://github.com/rslsp1/krita-codex-mcp.git
- `main`: unchanged upstream history.
- `rsl-master`: our reviewed integration branch and the source used locally.
- Feature branches: `codex/<feature>`, with PRs targeting `rsl-master` in our fork.

Create feature branches from the latest `origin/rsl-master`. Merge implementation PRs only after
explicit approval. Keep the upstream author attribution, license, and published package identity;
the original PyPI package does not contain unpublished fork changes.

## Bring in upstream changes

If `upstream` is missing, register it once:

```bash
git remote add upstream https://github.com/cyyprezz/krita-codex-mcp.git
```

Update the mirror branch without rewriting its history:

```bash
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main
```

Integrate upstream changes on a separate branch so conflicts and test results can be reviewed:

```bash
git fetch origin
git switch -c codex/sync-upstream origin/rsl-master
git merge origin/main
```

Resolve conflicts, run the checks below, push the branch and open a PR **in our fork**, with
base `rsl-master`. Merge after approval. Merge upstream history instead of rebasing or force
pushing the shared `rsl-master` branch.

## Local source and checks

```bash
git switch rsl-master
git pull --ff-only origin rsl-master
uv sync --locked --all-groups --python 3.12
uv run python scripts/verify_release.py
uv run python -m unittest discover -s tests -v
node --test tests/preview_app.test.cjs
uv run ruff check .
uv run python -m build
```

The UI tests require Node.js 18 or newer and use only its built-in test runner. For the full
wheel/STDIO/Codex discovery checks, follow `.github/workflows/ci.yml`.
Source launch instructions are in [document preview](document-preview.md).
