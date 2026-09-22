# Where the issue links came from — short version

Each flaky test → its fixing **PR** (from `only_accepted_idoft.csv`) → the PR's **issue report**
(read from the PR itself on GitHub via `gh api`).

The `Issue Report Source` column tells you exactly where each link was found:

| Source tag | Where it came from |
|------------|--------------------|
| `gh:closes` | GitHub's own "this PR closes issue #N" link (`closingIssuesReferences`) — GitHub-validated |
| `jira:title` | A JIRA key (e.g. `HIVE-28603`) written in the **PR title** |
| `jira:body-url` | A JIRA `/browse/KEY` link in the **PR body** |
| `gh:body-fixes` | A `Fixes/Closes/Resolves #N` typed in the **PR body** |
| `gh:body-url` | A full `github.com/<repo>/issues/N` URL pasted in the **PR body** |
| `jira:test-search` | Your original method — Apache JIRA text search by test name |

Trackers the links point to:
- **GitHub Issues** (`github.com/<repo>/issues/N`) — fastjson2, nacos, graylog, dubbo, apollo, etc.
- **JIRA servers** — `issues.apache.org` (HIVE, HADOOP…), `issues.redhat.com` (WFLY, UNDERTOW),
  `sakaiproject.atlassian.net` (SAK), `liquibase.atlassian.net` (CORE).

All links were verified to exist and to be real flaky-test bug reports (see `ISSUE_MAPPING_METHODS.md` §5).
