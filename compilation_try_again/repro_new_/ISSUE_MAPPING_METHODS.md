# Mapping Flaky-Test PRs to their Issue Reports — Methods & Results

**Date:** 2026-09-22
**Input files:** `test_config_issues_yes.csv`, `only_accepted_idoft.csv`, `test_config_jira.csv`
**Output file:** `test_config_issues_found.csv`

---

## 1. Goal

Each flaky test in `test_config_issues_yes.csv` was fixed by a pull request. We want the
**issue report** (bug ticket) that each PR addresses. The original approach (`jira.py`)
searched Apache JIRA by test name and found ~38 matches. This work adds a second,
complementary approach based on reading the PRs themselves, and merges the two.

## 2. Where to find the results

All results are in **`test_config_issues_found.csv`**. Columns:

| Column | Meaning |
|--------|---------|
| `Issue ID` | Your original issue id — **untouched** (this is "what you had before") |
| `PR Link` | The PR each row was matched through (the basis for the mapping) |
| `Match Status` | One of `new` / `already had` / `none found` — **filter on this** (see below) |
| `Issue Report` | **All** issue reports found for the row (old + new combined) |
| `Issue Report Source` | How each reference was found (provenance tag, see §4) |
| `New Issue Report(s)` | **Only** references not already in your `Issue ID` / `JIRA` columns |

Multiple references in one cell are separated by `;`, aligned position-for-position
between `Issue Report` and `Issue Report Source`.

### Reading `Match Status`

An empty `New Issue Report(s)` cell can mean two different things, so use `Match Status`
to tell them apart:

| `Match Status` | Rows | Meaning |
|----------------|------|---------|
| `new` | 155 | A new issue was found — it's in `New Issue Report(s)` |
| `already had` | 136 | An issue was found, but you already had it (`Issue Report` is filled, `New…` is empty) |
| `none found` | 761 | No issue report found for this test's PR (everything empty) |

**To see just the new matches, filter `Match Status = new`.** (155 rows, tracing to 40
distinct new issue references — many rows share the same PR/issue.)

## 3. The mapping pipeline

```
flaky test  ──(hop 1)──►  fixing PR  ──(hop 2)──►  issue report
```

**Hop 1 — test → PR.** Taken directly from `only_accepted_idoft.csv`, which already pairs
each accepted flaky test (col 4) with the PR that fixed it (col 7). Matched on the test
name, normalising `#` ↔ `.` between `Class#method` and `Class.method` forms.

**Hop 2 — PR → issue.** For each distinct PR (250 of them) we fetched the PR title and body
from GitHub via `gh api graphql`, then extracted issue references (see method table below).

Scripts:
- `fetch_prs.py` — fetches all PRs → `pr_cache.json`
- `build_output.py` — extraction + merge → `test_config_issues_found.csv`
- `verify_issues.py` — verification pass (§5)

## 4. Extraction methods (the `Issue Report Source` tags)

| Source tag | What it reads | Tracker type | Confidence |
|------------|---------------|--------------|------------|
| `gh:closes` | GitHub's validated `closingIssuesReferences` (the "Fixes #N" link GitHub itself recognises) | GitHub Issues | **Highest** — GitHub confirms the PR closes the issue |
| `jira:title` | A JIRA key (`PROJ-1234`) in the **PR title** | JIRA | High — developer named the ticket |
| `jira:body-url` | A JIRA `/browse/KEY` URL in the **PR body** | JIRA | High |
| `gh:body-fixes` | A `Fixes/Closes/Resolves #N` typed in the **PR body** (not auto-linked by GitHub) | GitHub Issues | High |
| `gh:body-url` | A full `github.com/<repo>/issues/N` URL pasted in the **PR body** (same repo only) | GitHub Issues | Medium — could cite a related issue; verified in §5 |
| `jira:test-search` | **Your original method** — Apache JIRA full-text search by test name (merged in from `test_config_jira.csv`) | Apache JIRA | Medium |

### Why two kinds of tracker?

Non-Apache projects don't share one issue tracker:
- **GitHub Issues** — the report lives at `github.com/<repo>/issues/<N>` (fastjson2, nacos,
  graylog, OpenRefine, dubbo, pinot, apollo, snowflake-jdbc, …). Also several Apache-donated
  projects that migrated to GitHub Issues (dubbo, shardingsphere, seatunnel, seata, pinot).
- **JIRA servers** — a separate site keyed like `PROJ-1234`. Four appear in this data:
  `issues.apache.org/jira` (HIVE, HADOOP, HBASE, FLINK, LANG, AMQ, NIFI…),
  `issues.redhat.com` (WFLY, WFCORE, UNDERTOW), `sakaiproject.atlassian.net` (SAK),
  `liquibase.atlassian.net` (CORE).

The original test-name search only queried `issues.apache.org`, so it structurally could not
reach GitHub-hosted trackers or the non-Apache JIRA servers. The PR-based method sidesteps
this by following whatever link the developer actually put in the PR.

## 5. Verification — checking for wrong mappings

Every one of the 36 distinct GitHub-issue references was fetched back from GitHub
(`verify_issues.py`) to confirm it (a) exists and (b) is genuinely a flaky-test / non-
determinism bug report. Results:

- **36 / 36 GitHub issues confirmed** as real flaky-test reports (titles such as
  *"Flaky test …"*, *"Test can fail if HashMap iterates in a different order"*,
  *"Non-Determinism In Unit Test"*). All resolve; none are 404.
- **2 false positives were found and removed.** Both came from PR-template boilerplate
  inside `<!-- ... -->` HTML comments:
  - `seata/seata/issues/97` — the template example text `add "fixes #xxx" … for example, fixes #97`.
  - `apache/seatunnel/issues/4544` — a contribution-guidelines link inside a comment.
  The extractor now strips HTML comments before reading the body, eliminating this class of error.

**JIRA keys** from `jira:title` / `jira:body-url` are taken verbatim from the developer's own
PR title/body, so the key is what the author explicitly cited. The 4 keys that are new to
this work were each traced to a specific PR (§6).

**Confidence note:** `gh:closes`, `jira:title`, `jira:body-url`, `gh:body-fixes` are
high-confidence. `gh:body-url` and `jira:test-search` are strong but occasionally point at a
*related* rather than the exact issue — the `Issue Report Source` column lets you filter by
confidence if you want a stricter subset.

## 6. Results summary

| Metric | Value |
|--------|-------|
| Distinct PRs examined | 250 |
| Distinct PRs with an issue found | 64 |
| Test rows with an issue report | 291 |
| Distinct issue references (total) | 83  (47 JIRA keys + 36 GitHub issues) |
| **Genuinely new** (not in your `Issue ID` or `JIRA` columns) | **40** |
| — new JIRA keys | 4 — `AMQ-9395`, `CORE-3596`, `LANG-1500`, `LANG-1501` |
| — new GitHub issues | 36 |

The main contribution over the original method is the **36 GitHub-hosted issue reports**
(non-Apache and migrated-Apache projects), which the Apache-only text search could not reach.
The JIRA side was already well covered by the original method; the PR approach mostly
re-confirmed it and added 4 keys on other trackers.

### The 4 new JIRA keys and their source PRs

| Key | Tracker | Found via | PR |
|-----|---------|-----------|----|
| `AMQ-9395` | issues.apache.org/jira/browse/AMQ-9395 | JIRA URL in PR body | apache/activemq#1129 |
| `CORE-3596` | liquibase.atlassian.net/browse/CORE-3596 | key in PR title | liquibase/liquibase#1005 |
| `LANG-1500` | issues.apache.org/jira/browse/LANG-1500 | key in PR title | apache/commons-lang#480 |
| `LANG-1501` | issues.apache.org/jira/browse/LANG-1501 | key in PR title | apache/commons-lang#481 |
