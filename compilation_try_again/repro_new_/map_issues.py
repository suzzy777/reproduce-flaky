#!/usr/bin/env python3
"""
map_issues.py  --  Map flaky-test PRs to their issue reports, end to end.

Pipeline:
  flaky test  --(hop 1)-->  fixing PR  --(hop 2)-->  issue report

  Hop 1 (test -> PR):   join on test name against only_accepted_idoft.csv
  Hop 2 (PR -> issue):  fetch each PR from GitHub (gh api graphql) and pull
                        the issue it references (JIRA key in title/body,
                        GitHub closing refs, "Fixes #N", issue URLs).
  Merge:                union with the original JIRA text-search results.

Requirements: python3, and the GitHub CLI `gh` authenticated (`gh auth status`).

Usage:  python3 map_issues.py
Outputs: test_config_issues_found.csv   (+ pr_cache.json to avoid re-fetching)
"""

import csv, json, os, re, subprocess, sys

# ---------------------------------------------------------------- config
TESTS_FILE    = "test_config_issues_yes.csv"   # rows to annotate (has flaky_test, Issue ID)
IDOFT_FILE    = "only_accepted_idoft.csv"      # test name (col 4) -> PR link (col 7)
JIRA_FILE     = "test_config_jira.csv"         # optional: original JIRA text-search column
OUTPUT_FILE   = "test_config_issues_found.csv"
CACHE_FILE    = "pr_cache.json"                # cached PR title/body/closes

# JIRA-key prefixes that are really encodings / licenses / algorithms, not tickets
BLOCK = {'LICENSE','GPL','LGPL','AGPL','XPYEYD','UTF','SHA','MD','HTTP','JDK','ISO',
         'ASCII','BASE','TLS','SSL','JSR','RFC','GB','AES','RSA','EC','CVE','SPDX',
         'BSD','MIT','APACHE','CC','X','P','H'}
JIRA_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,9})-(\d+)\b')

def norm(t):
    """Normalise a test name so Class#method and Class.method compare equal."""
    return t.strip().replace('#', '.')

# ---------------------------------------------------------------- hop 1: test -> PR
def load_test_to_pr(path):
    m = {}
    with open(path, encoding='utf-8-sig') as f:
        r = csv.reader(f); next(r)
        for row in r:
            if len(row) < 11:
                continue
            m.setdefault(norm(row[3]), row[6].strip())   # col4 test -> col7 PR
    return m

# ---------------------------------------------------------------- hop 2: fetch PRs
GQL = """
query($owner:String!, $name:String!, $num:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$num) {
      title
      body
      closingIssuesReferences(first:10){ nodes { number url title } }
    }
  }
}"""
PR_RE = re.compile(r'github\.com/([^/]+)/([^/]+)/pull/(\d+)')

def fetch_prs(pr_links):
    cache = {}
    if os.path.exists(CACHE_FILE):
        cache = json.load(open(CACHE_FILE))
    todo = [p for p in pr_links if p and p not in cache]
    for i, pr in enumerate(todo, 1):
        m = PR_RE.search(pr)
        if not m:
            cache[pr] = {"error": "unparseable"}; continue
        owner, name, num = m.groups()
        try:
            out = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={GQL}',
                 '-F', f'owner={owner}', '-F', f'name={name}', '-F', f'num={num}'],
                capture_output=True, text=True, timeout=60)
            p = json.loads(out.stdout).get('data', {}).get('repository', {}).get('pullRequest')
            if p is None:
                cache[pr] = {"error": (out.stdout or out.stderr)[:200]}
            else:
                cache[pr] = {
                    "title": p.get("title") or "",
                    "body":  p.get("body") or "",
                    "closes": [{"url": n["url"]} for n in p["closingIssuesReferences"]["nodes"]],
                }
        except Exception as e:
            cache[pr] = {"error": str(e)[:200]}
        if i % 20 == 0:
            print(f"  fetched {i}/{len(todo)}", file=sys.stderr)
            json.dump(cache, open(CACHE_FILE, 'w'))
    json.dump(cache, open(CACHE_FILE, 'w'))
    return cache

# ---------------------------------------------------------------- extract issue refs from a PR
def extract(pr, meta):
    """Return [(issue_ref, source_tag), ...] for one PR, priority-ordered."""
    if not meta or 'error' in meta:
        return []
    repo  = PR_RE.search(pr).group(0).split('/pull')[0].split('github.com/')[1]
    title = meta.get('title', '') or ''
    body  = meta.get('body', '') or ''
    # Strip HTML comments: PR templates hide example refs ("fixes #97") in them.
    body = re.sub(r'<!--.*?-->', ' ', body, flags=re.S)

    out = []
    # 1. JIRA key in title
    for pfx, num in JIRA_RE.findall(title):
        if pfx not in BLOCK:
            out.append((f'{pfx}-{num}', 'jira:title'))
    # 2. JIRA browse URL in body
    for key in re.findall(r'jira[^\s"\')]*?/browse/([A-Z][A-Z0-9]{1,9}-\d+)', body):
        out.append((key, 'jira:body-url'))
    # 3. GitHub validated closing references
    for n in meta.get('closes', []):
        out.append((n['url'], 'gh:closes'))
    # 4. Same-repo GitHub issue URL in body
    for m in re.findall(r'github\.com/([^/]+/[^/]+)/issues/(\d+)', body):
        if m[0].lower() == repo.lower():
            out.append((f'https://github.com/{m[0]}/issues/{m[1]}', 'gh:body-url'))
    # 5. "Fixes #N" in body -> same-repo issue
    for num in re.findall(r'(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)', body, re.I):
        out.append((f'https://github.com/{repo}/issues/{num}', 'gh:body-fixes'))

    seen, uniq = set(), []
    for ref, src in out:
        if ref not in seen:
            seen.add(ref); uniq.append((ref, src))
    return uniq

# ---------------------------------------------------------------- optional: original JIRA search
def load_old_jira(path):
    m = {}
    if not os.path.exists(path):
        return m
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            keys = [k.strip() for k in row.get('JIRA', '').split(';') if k.strip()]
            if keys:
                m[norm(row['flaky_test'])] = keys
    return m

# ---------------------------------------------------------------- main
def main():
    test_to_pr = load_test_to_pr(IDOFT_FILE)

    # distinct PRs referenced by the tests we care about
    wanted = set()
    with open(TESTS_FILE, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            pr = test_to_pr.get(norm(row['flaky_test']))
            if pr:
                wanted.add(pr)
    print(f"{len(wanted)} distinct PRs to resolve", file=sys.stderr)

    cache    = fetch_prs(sorted(wanted))
    pr_issue = {pr: extract(pr, cache.get(pr)) for pr in wanted}
    old_jira = load_old_jira(JIRA_FILE)

    # what the user already had = original Issue ID column + original JIRA column
    had = set()
    with open(TESTS_FILE, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row.get('Issue ID', '').strip():
                had.add(row['Issue ID'].strip())
    for ks in old_jira.values():
        had.update(ks)

    with open(TESTS_FILE, encoding='utf-8-sig') as fin, \
         open(OUTPUT_FILE, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        fields = reader.fieldnames + ['PR Link', 'Match Status',
                                      'Issue Report', 'Issue Report Source', 'New Issue Report(s)']
        w = csv.DictWriter(fout, fieldnames=fields); w.writeheader()

        stats = {'new': 0, 'already had': 0, 'none found': 0}
        for row in reader:
            t  = norm(row['flaky_test'])
            pr = test_to_pr.get(t, '')
            cands = list(pr_issue.get(pr, []))
            for k in old_jira.get(t, []):
                cands.append((k, 'jira:test-search'))
            seen, uniq = set(), []
            for ref, src in cands:
                if ref not in seen:
                    seen.add(ref); uniq.append((ref, src))

            newrefs = [ref for ref, _ in uniq if ref not in had]
            status  = 'none found' if not uniq else ('new' if newrefs else 'already had')
            stats[status] += 1

            row['PR Link']              = pr
            row['Match Status']         = status
            row['Issue Report']         = ';'.join(ref for ref, _ in uniq)
            row['Issue Report Source']  = ';'.join(src for _, src in uniq)
            row['New Issue Report(s)']  = ';'.join(newrefs)
            w.writerow(row)

    print(f"Wrote {OUTPUT_FILE}", file=sys.stderr)
    print(f"  new={stats['new']}  already had={stats['already had']}  none found={stats['none found']}",
          file=sys.stderr)

if __name__ == '__main__':
    main()
