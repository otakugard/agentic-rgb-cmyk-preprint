# gitleaks audit — PR #2

Date: 2026-11-19
Tool: gitleaks 8.30.1 (Homebrew)
Scope: PR #2, `Make a colour contract the single colour owner`
Commit range: `200250d..71b555f`
Result: **PASS** — no secrets in the PR or in the repository history.

## Commands and results

```text
$ gitleaks git . --log-opts="200250d..71b555f" --redact
1 commits scanned.
scanned ~117099 bytes (117.10 KB) in 68.1ms
no leaks found

$ gitleaks git . --log-opts="--all" --redact
10 commits scanned.
scanned ~622173 bytes (622.17 KB) in 70.4ms
no leaks found
```

The PR range contains one commit (`4908cf6`); the merge commit has no own
changes. The full-history scan covers every commit in `main` and its merged
branches.

## Working-tree scan

```text
$ gitleaks dir . --redact
leaks found: 2
```

Both hits are `generic-api-key` in `config/color-contract.json`:

| Line | Field | Classification |
|---|---|---|
| 6 | `figma.fileKey` | Figma file identifier, not a credential |
| 15 | `figma.variableKey` | Figma variable identifier, not a credential |

`config/color-contract.json` is git-ignored, was never tracked
(`git log --all -- config/color-contract.json` is empty), and is not in PR #2.
The scan matched two 22/40-character identifiers, not an API token. No value is
reproduced in this report.

The working-tree scan is not part of the PR result. If `gitleaks dir` is used
routinely, expect these two known false positives or add a path allowlist for
the ignored local contract. Do not allowlist the file in a way that would hide
a force-added contract.

## Scope checks

PR #2 changes 30 files. None of them is an ICC profile, a local colour
contract, a probe cache, a snapshot, a job directory, `__pycache__`, or
`.DS_Store`:

```text
$ git diff --name-only 200250d..71b555f | grep -Ei '\.icc$|color-contract\.json$|probe-cache|figma-|\.DS_Store|__pycache__|jobs/'
none
```

The committed template `config/color-contract.example.json` contains
placeholder hashes (`replace-with-...`), no profile bytes and no client
values.

## Privacy review

PR #2 contains the project brand tuple in `config/color-contract.example.json`,
`README.md`, `scripts/13-rca-selftest.py` and
`tests/rca-regression-selftest.json`. The owner reviewed this on 2026-11-19 and
confirmed that the brand colour values are **not confidential**. They are not a
leak, and no history rewrite or value replacement is required.

## Limits

- gitleaks detects credential-shaped strings. It does not prove the absence of
  confidential business data. The separate scan for client names and private
  Figma identifiers is documented in `audits/2026-11-19-colour-contract.md`.
- No rotation is needed: no credential was committed.
- This audit covers git content. GitHub secret-scanning alerts and third-party
  service logs are separate.
