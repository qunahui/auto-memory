# Copilot CLI Upgrade Smoke Test

After every Copilot CLI upgrade, run these checks to verify `session-recall` still works with the new schema.

## When to run

After every `npm i -g @github/copilot` or `brew upgrade copilot-cli`.

## Steps

### 1. Record old version

```bash
copilot --version
```

### 2. Backup session-store.db

```bash
cp ~/.copilot/session-store.db ~/.copilot/session-store.db.bak-$(date +%Y%m%d)
```

### 3. Upgrade

```bash
npm i -g @github/copilot
# or: brew upgrade copilot-cli
```

### 4. Confirm new version

```bash
copilot --version
```

### 5. Schema check (MUST exit 0)

```bash
cd auto-memory && session-recall schema-check
```

If this fails with exit code 2:
- **Schema drift detected.** The Copilot CLI upgrade changed the DB schema.
- Run `sqlite3 ~/.copilot/session-store.db '.schema sessions'` to see the new schema.
- Update `EXPECTED_SCHEMA` in `src/session_recall/db/schema_check.py` to match.
- Or rollback: `npm i -g @github/copilot@<old-version>`

### 6. Smoke test — list

```bash
session-recall list --json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d[\"count\"]} sessions')"
```

### 7. Smoke test — health

```bash
session-recall health --json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'overall: {d[\"overall_score\"]}')"
```

### 8. Log result

Append to `~/.copilot/upgrade-log.md`:

```
## YYYY-MM-DD — Copilot CLI vX.Y.Z → vA.B.C
- schema-check: PASS/FAIL
- list: PASS/FAIL
- health: PASS/FAIL (score: N.N)
```

## If schema drift detected

1. Pin previous version: `npm i -g @github/copilot@<old-version>`
2. Open an issue or update `EXPECTED_SCHEMA` in `schema_check.py`
3. Do NOT use the new Copilot CLI with a broken `session-recall`
