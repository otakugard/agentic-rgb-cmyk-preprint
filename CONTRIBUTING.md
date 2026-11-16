# Contributing

This project is a prepress pipeline. A wrong PASS is worse than a loud FAIL.

## Ground rules

- Do not weaken a check to make a job pass.
- Do not add a rule that cannot be tested against a file.
- Do not commit client artwork, client names, job directories, ICC profiles, or
  credentials.
- Keep the single-conversion-point contract: RGB to CMYK happens once, in
  `01-prepare.sh`, with explicit ICC parameters.
- Every new check needs a negative fixture. Prove the check fails on the fixture
  before it passes on the good file.

## Changing a gate

1. Add or extend a fixture under `tests/`.
2. Prove the checker fails on the fixture.
3. Prove the checker passes on the known-good file.
4. Update `docs/gates.md` with the new evidence.
5. Keep `gate3.json` / `qa.json` schema fields stable. Downstream tools read
   `overall`, `counts`, `checks`, `sha256`, `aiSha256`, `acceptanceHash`.

## Changing a script

Run:

```bash
python3 -m py_compile scripts/*.py scripts/lib/*.py
for f in scripts/*.sh scripts/lib/*.sh install/*.sh; do bash -n "$f"; done
./install/verify.sh --full
```

The pull request should paste the verify output.

## Docs style

Prose follows [`docs/writing-rules.md`](docs/writing-rules.md): affirmative
phrasing, short sentences, one owner per repeated rule.

## Reporting a result

Use the status vocabulary: PASS, FAIL, PENDING, WARN. State the evidence file.
Report ICC-predicted DeltaE00 as a prediction. Call a candidate PDF a candidate;
delivery needs Gate 3 PASS and the print shop's written approval.
