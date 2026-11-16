# Documentation writing rules

These rules govern English prose in this repository: README, `docs/`,
`CONTRIBUTING.md`, install prompts, and script headers. They remove
machine-written tells without weakening a technical claim.

Apply them to new text and to edited paragraphs. Do not restyle a whole file to
satisfy a count.

Provenance: the rules combine a 629-document human/model comparison and a 270-pair
same-fact retest (Chinese output), the Wikipedia "Signs of AI writing" catalogue,
and five project-level prohibitions on hedging. The Chinese figures are
directional for English. No English measurement exists yet; do not present these
rules as an English significance result.

---

## 1. The test for one sentence

Ask two questions.

1. Does the sentence state what exists, what happens, or what the reader must do?
2. Can the reader act on it without inferring the opposite?

Lead with the affirmative. State a limit as a named state, an owner, or a
condition. A sentence that defines a thing by what it is not fails both tests.

---

## 2. Rules

| # | Rule | Required form |
|---|---|---|
| R1 | Lead affirmative | Subject, verb, object. Put the judgement first, then the reason |
| R2 | Name the state | `PASS` states its own coverage. The neighbouring states stay unnamed |
| R3 | Replace disclaimers with scope plus owner | "Gate 3 covers file structure. The print shop decides acceptance in writing." |
| R4 | No equivalence denials | State what the evidence establishes. State the remaining step |
| R5 | No single-factor or liability disclaimers | Name the condition that changes the outcome: "If the ICC changes, rerun 06" |
| R6 | No tailing negations | Write the clause: "Ghostscript is the only conversion point; every change is an explicit input" |
| R7 | No signposting, no openers | Delete "Note that", "It is important to note", "In short", "Simply put" |
| R8 | One action per sentence, 20 words or fewer, active voice, simple present | "07c builds the candidate PDF. It reads only `cmyk-final.pdf`" |
| R9 | One name per thing | Reuse the project term. Do not cycle synonyms to avoid repetition |
| R10 | Report, do not announce | Delete lines that describe the document instead of the system |
| R11 | Describe the system, not the edit | "The version check accepts 2024 and newer." Changelogs and audits are the exception |
| R12 | Keep the skeleton | Heading hierarchy, numbering, lists, tables, and code blocks stay in place |
| R13 | Prohibition needs a cause and an alternative | "Do not force quit; probe with `get version`, then rerun" |
| R14 | Number, unit, source | "TAC 300%" or "0.2 mm". A predicted value carries its method |
| R15 | One canonical owner | A rule lives in one file. Other files link to it |

---

## 3. Forbidden forms

Every example below is quoted as a defect, not as recommended text.

### R1 and R6: negative parallelism and tailing negation

| Defect | Fix |
|---|---|
| "A check passes because the script ran, not because the file is correct." | "A check passes because the script ran. The file can still be wrong." |
| "These are measured behaviours, not guesses." | "These are measured behaviours." |
| "an explicit input rather than a nearest-match guess" | "an explicit input; the match step reads the file" |
| "…and no silent fixes" | "…and every change is an explicit input" |
| "extraction, not `fills`" | "object-level content-stream extraction" |
| "The truth is not X, but Y." | "The result is Y." |

The English signal is the same construction as the Chinese one. In the 629-document
comparison it ran at 0.73 per 1k words against 0.22 for human text; the 270-pair
retest kept the direction on both models. Treat the count as a filter for
candidate sentences, not as proof that a text was generated.

### R2 and R3: equivalence denials and disclaimers

| Defect | Fix |
|---|---|
| "A candidate PDF is not a final delivery." | "A candidate PDF stays a candidate. Delivery needs Gate 3 PASS and the print shop's written approval." |
| "Gate 3 PASS does not mean the print shop accepted the file." | "Gate 3 covers output structure. The print shop decides acceptance, in writing." |
| "`ok: true` only means the script completed. It is not acceptance." | "`ok: true` reports that the script completed. `qa.json` carries acceptance." |
| "It does not mean:" followed by a list | "What stays outside Gate 3:" with the owner named on each line |
| "This does not equate to / imply / represent …" | "This establishes …" plus the step that remains |
| "cannot assert / does not necessarily / no guarantee" | "If the ICC changes, rerun 06. If the proof is unavailable, the check stays PENDING." |

The Chinese figures for the colon-and-conclusion form are 2.1 times human use.
For English, the reliable test is grammatical: a clause that exists only to
prevent a misreading becomes a scope sentence with an owner.

### R4 and R5: hedging an established result

| Defect | Fix |
|---|---|
| "The fixture fails to prove the checker works." | "The fixture makes the checker fail. That is the pass condition." |
| "The sample is insufficient to demonstrate coverage." | "The sample covers 12 of 14 cases; the other 2 stay PENDING." |
| "You cannot rely solely on the probe." | "Run the probe and the plate check. Both must agree." |

The Dutch-book habit of listing what a check cannot do belongs in a scope table
with a named next step. An honest project states its coverage; it does not chant
its limits in the running text.

### R7, R10, R11: signposting, announcement, edit log

| Defect | Fix |
|---|---|
| "Note that the first run writes `prepare.json`." | "The first run writes `prepare.json`." |
| "In this section we describe the gates." | Delete the sentence. Start with Gate 1 |
| "This section has been rewritten to clarify reruns." | Describe the rerun rule itself |
| "Let's dive into the pipeline." | "The pipeline has ten steps." |

`git log` records edits. Documentation describes the system as it stands.

### R8: sentence shape

Target 20 words. One action per sentence. Active voice. Simple present. Name the
actor.

Avoid the `-ing` tail: ", ensuring …", ", allowing …", ", highlighting …". The
construction adds a fake second layer and hides the actor.

Avoid the subjectless fragment. "No configuration file needed" becomes "The job
runs without a configuration file."

### R9: synonyms

Name a thing once and reuse the name. `cmyk-final.pdf` stays `cmyk-final.pdf`;
it does not become "the scaled intermediate", "the final intermediate", and "the
print PDF" in three paragraphs. The 270-pair retest measured 0.83 to 0.86 renames
per paragraph in model text against 0.49 in human text. Fix the name, not the
sentence.

### R12 and typography

- Headings in sentence case. No Title Case.
- No emoji.
- No bold as decoration. Bold marks a term on first use or a status word.
- Straight quotes and ASCII hyphens. The en dash stays only in a numeric range
  such as "bytes 16–19".
- No em dash as punctuation. Use a period, a comma, or a colon.

---

## 4. Negatives that survive

A negative is a defect when it replaces a statement. It stays when it *is* the
contract. Allowed forms:

1. **State semantics.** "`PENDING`: not generated, not verified, or needs a human
   decision." A status table defines absence by design.
2. **Immutable inputs.** "`source/` is never modified." A one-line boundary with a
   named directory.
3. **Prohibitions with cause and alternative** (R13).
4. **Error text and test fixtures.** The message the user sees, and the string a
   check matches, stay verbatim.
5. **Measured absence.** "Zero RGB operators in the page content stream." A count
   of zero is data.

Everything else becomes an affirmative sentence.

---

## 5. Duplication and ownership

One rule, one file. Before adding a paragraph, find the owner.

| Content | Owner |
|---|---|
| Gate definitions and status semantics | `docs/gates.md` |
| Step inputs, outputs, rerun order | `docs/pipeline.md` |
| Measured failures and their handling | `docs/limitations.md` |
| Install checks and agent prohibitions | `docs/install-agent.md` |
| Requirements and versions | `README.md` |
| ICC acquisition | `color/profiles/README.md` |

`README.md` summarises and links. A prompt block appears once in the repository;
every other copy is a link. Two copies drift, and the drift is silent.

---

## 6. Mechanical scan

Run the counts before and after a rewrite. Report the number, the file, and the
line.

| Metric | Target | Baseline at introduction |
|---|---|---|
| Negated-sentence share | 20% or less | 32% over 4,871 words |
| Negation tokens per 1k words | 15 or less | 28.7 |
| Em dashes | 0 | Numeric ranges excepted |
| "does not mean / equate / imply / guarantee" | 0 | R3 |
| "not X but Y", "rather than" in a definition | 0 | R1 |
| "cannot assert / not necessarily" | 0 | R5 |
| Sentences over 25 words | 10% or less | R8 |
| Duplicate paragraphs across files | 0 | R15 |

These counts detect candidates. A human decides. A sentence can carry a
legitimate negation and still fail R2.

---

## 7. Review workflow

1. **Read.** Load the file, the owner file, and the tests that read the text.
2. **Classify.** Mark each paragraph KEEP, MERGE, MOVE, or DELETE. MOVE only when
   the target file already carries the rule in full.
3. **Rewrite.** Apply R1 to R15. Keep identifiers, paths, commands, numbers, status
   words, and test anchors byte-identical.
4. **Audit.** Compare old and new in both directions. Every removed sentence maps
   to a rule or to an owner that still states it.
5. **Verify.** Re-run the scan, the link check, and any test that greps the docs.
   Report the counts and the remaining gaps.
6. **Freeze.** Stop after PASS. Further edits are cosmetic and cost review time.

A rewrite that drops a limit, an owner, or a requirement has failed, even when
every sentence is affirmative.
