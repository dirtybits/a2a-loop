# a2a-loop

Headless Codex <-> Claude Code workflow coordinator.

The loop uses GitHub PR state as the shared coordination layer:

1. Claude plans.
2. Codex implements and opens/updates a PR.
3. Claude reviews.
4. Codex addresses review comments.
5. Claude approves with `MERGE_DECISION: APPROVE`.
6. The coordinator can squash-merge when `--merge` is passed.

## Requirements

- `codex` CLI with `codex exec`
- `claude` CLI with `claude -p`
- `gh` GitHub CLI authenticated for the target repo
- A target repository with an `origin` remote

## Dry Run

Always start with `--dry-run`:

```bash
./a2a-loop.py \
  --repo /path/to/repo \
  --base main \
  --goal "Implement the feature..." \
  --max-rounds 3 \
  --dry-run
```

## Real Run

```bash
./a2a-loop.py \
  --repo /path/to/repo \
  --base main \
  --goal "Implement the feature..." \
  --max-rounds 3
```

Add `--merge` only when you want the coordinator to squash-merge after Claude emits the exact approval token.

## Safety

- Run on a clean branch or disposable worktree.
- Keep `--max-rounds` bounded.
- Do not pass `--merge` until the dry-run and prompt contract look right.
- The coordinator fails closed unless Claude emits `MERGE_DECISION: APPROVE`.
- Logs are written to `a2a-logs/<timestamp>/run.log`.

