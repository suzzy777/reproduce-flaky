#!/usr/bin/env python3
"""Reproduce order-dependent (OD) flaky tests from the high-confidence CSV.

For each victim test we look up how it was compiled (module / strategy / Java
version) in repaired_tests_523.csv, check out the repo at that SHA, build it,
then run two Maven invocations and check the expected pass/fail pattern:

  * OD / OD-Vic (victim polluted by polluter):
      - order run  `Polluter, Victim`  -> expected FAIL
      - alone run  `Victim`            -> expected PASS
  * OD-Brit (brittle victim):
      - alone run  `Victim`            -> expected FAIL
      - order run  `Polluter, Victim`  -> expected PASS

A row is "REPRODUCED" only when both runs match their expectation.

Reuses compile_all.py for Java discovery + POM strategy patching.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compile_all  # noqa: E402  (discover_java_home, apply_strategy, SKIP_PROPERTIES)

# repaired_tests_523.csv column indices (no header row)
COL_REPO, COL_SHA, COL_MODULE, COL_TEST = 0, 1, 2, 3
COL_CATEGORY, COL_STRATEGY, COL_JAVA = 4, 12, 13

def run(cmd, cwd=None, env=None, timeout=None):
    """Run a command, capture combined output, never raise on non-zero."""
    try:
        p = subprocess.run(
            list(cmd), cwd=cwd, env=env, text=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.output or "") + f"\n[TIMEOUT after {timeout}s]\n"
    except FileNotFoundError as exc:
        return 127, f"[NOT FOUND: {exc}]\n"


def checkout(repo, sha, cache_dir, work):
    """Mirror-clone (cached per repo) then make a detached worktree at sha."""
    mirror = cache_dir / (hashlib.sha256(repo.encode()).hexdigest()[:16] + ".git")
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not mirror.exists():
        code, out = run(["git", "clone", "--mirror", repo, str(mirror)])
        if code:
            raise RuntimeError(f"git clone failed:\n{out[-2000:]}")
    if work.exists():
        shutil.rmtree(work)
    code, out = run(["git", "clone", "--shared", "--no-checkout", str(mirror), str(work)])
    if code:
        raise RuntimeError(f"work clone failed:\n{out[-2000:]}")
    code, out = run(["git", "checkout", "--detach", sha], cwd=work)
    if code:  # sha not in mirror yet -> fetch it specifically
        run(["git", "--git-dir", str(mirror), "fetch", "origin", sha])
        code, out = run(["git", "checkout", "--detach", sha], cwd=work)
        if code:
            raise RuntimeError(f"cannot checkout {sha}:\n{out[-2000:]}")


def to_surefire(name):
    """`pkg.Class.method` -> `pkg.Class#method`; a bare class stays as-is."""
    name = name.strip()
    last = name.split(".")[-1]
    if last and last[0].islower():          # lowercase last token == method
        i = name.rfind(".")
        return name[:i] + "#" + name[i + 1:]
    return name                              # whole class (e.g. J2CacheTester)


def classify(order_res, alone_res):
    """Map the two observed run results to (observed_pattern, verdict).

    Works for plain 'OD' too: we don't assume a direction, we just report
    whichever order-dependence actually showed up.
    """
    if order_res is None or alone_res is None:
        return "", "INCONCLUSIVE"
    if order_res == "FAIL" and alone_res == "PASS":
        return "VICTIM", "REPRODUCED"       # polluter breaks an otherwise-good test
    if order_res == "PASS" and alone_res == "FAIL":
        return "BRITTLE", "REPRODUCED"      # test needs the other one to run first
    if order_res == "PASS" and alone_res == "PASS":
        return "NO_DEP", "NOT_REPRODUCED"   # no order dependence seen
    return "ALWAYS_FAIL", "NOT_REPRODUCED"  # fails both ways -> not an OD signal


def clean_reports(work):
    """Delete old surefire-reports so we never read a stale run's XML."""
    for d in work.rglob("surefire-reports"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def victim_status(work, victim_fqn):
    """PASS / FAIL / None for the specific victim method, read from the
    surefire XML report (TEST-<class>.xml).

    Modeled on parse_surefire_report.py: a <testcase> counts as failed if it
    has a <failure> or <error> child; a class-init failure shows up as a
    testcase whose name == classname (or empty) and is treated as the whole
    class failing.  Reading the victim's own method (not the run-wide summary)
    is what lets us tell the victim apart from the polluter in the order run.
    """
    cls, method = victim_fqn.rsplit(".", 1)
    reports = sorted(work.rglob(f"TEST-{cls}.xml"), key=lambda p: p.stat().st_mtime)
    if not reports:
        return None
    try:
        root = ET.parse(reports[-1]).getroot()   # newest report for this class
    except ET.ParseError:
        return None

    class_level_fail = False
    for tc in root.iter("testcase"):
        name = tc.get("name", "")
        failed = tc.find("failure") is not None or tc.find("error") is not None
        if name == tc.get("classname") or name == "":     # class-init / constructor failure
            class_level_fail = class_level_fail or failed
            continue
        if name == method or name.startswith(method + "["):  # exact or parameterized
            return "FAIL" if failed else "PASS"
    if class_level_fail:
        return "FAIL"
    return None   # victim method never ran


def mvn_base(work):
    return ["bash", str(work / "mvnw")] if (work / "mvnw").is_file() else ["mvn"]


def compile_project(work, module, java_home, maven_repo, timeout):
    cmd = mvn_base(work) + ["-B", "clean", "install", "-DskipTests", "-U"]
    cmd += compile_all.module_arguments(module, work)          # adds -pl/-am (or [] for '.')
    cmd += [f"-D{p}" for p in compile_all.SKIP_PROPERTIES]
    if maven_repo:
        cmd.append(f"-Dmaven.repo.local={maven_repo}")
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = f"{java_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return run(cmd, cwd=work, env=env, timeout=timeout)


def run_tests(work, module, test_ids, ordered, victim_fqn, java_home, maven_repo, timeout):
    cmd = mvn_base(work) + ["-B", "test", "-DfailIfNoTests=false"]
    if module not in (".", ""):
        cmd += ["-pl", module]
    if ordered:
        cmd.append("-Dsurefire.runOrder=testorder")
    cmd.append("-Dtest=" + ",".join(test_ids))
    cmd += [f"-D{p}" for p in compile_all.SKIP_PROPERTIES]
    if maven_repo:
        cmd.append(f"-Dmaven.repo.local={maven_repo}")
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = f"{java_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    clean_reports(work)                       # avoid reading a stale report
    run(cmd, cwd=work, env=env, timeout=timeout)
    return victim_status(work, victim_fqn), shlex.join(cmd)


def load_repaired(path):
    """test name -> (repo, sha, module, strategy, java)."""
    lookup = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) <= COL_JAVA:
                continue
            lookup[row[COL_TEST]] = (
                row[COL_REPO], row[COL_SHA], row[COL_MODULE],
                row[COL_STRATEGY].strip(), row[COL_JAVA].strip(),
            )
    return lookup


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--high-confidence", default=str(HERE / "od_polluting_tests_high_confidence.csv"))
    ap.add_argument("--repaired", default=str(HERE / "repaired_tests_523.csv"))
    ap.add_argument("--work-dir", default=str(HERE / "od_verify_work"))
    ap.add_argument("--out", default=str(HERE / "od_verify_results.csv"))
    ap.add_argument("--maven-repo", default="", help="optional -Dmaven.repo.local path")
    ap.add_argument("--java8", default=None)
    ap.add_argument("--java11", default=None)
    ap.add_argument("--java17", default=None)
    ap.add_argument("--compile-timeout", type=int, default=2400)
    ap.add_argument("--test-timeout", type=int, default=900)
    ap.add_argument("--limit", type=int, default=0, help="only process first N victims")
    ap.add_argument("--dry-run", action="store_true", help="print the orderings, run no Maven")
    args = ap.parse_args()

    work_root = Path(args.work_dir)
    cache_dir = work_root / "_cache"
    maven_repo = str(Path(args.maven_repo).expanduser().resolve()) if args.maven_repo else ""
    java_explicit = {8: args.java8, 11: args.java11, 17: args.java17}

    repaired = load_repaired(args.repaired)

    # Load victims and attach subject info; group by subject so we compile once.
    groups: dict[tuple, list[dict]] = {}
    with open(args.high_confidence, newline="", encoding="utf-8") as f:
        victims = list(csv.DictReader(f))
    if args.limit:
        victims = victims[: args.limit]

    for v in victims:
        test = v["Fully-Qualified Test Name"]
        info = repaired.get(test)
        if not info:
            print(f"SKIP (not in repaired csv): {test}")
            continue
        repo, sha, module, strategy, java = info
        key = (repo, sha, module, strategy, java)
        groups.setdefault(key, []).append({
            "test": test,
            "category": v["Category"],
            "polluters": [p for p in (x.strip() for x in v["Polluting Test(s)"].split("/")) if p],
        })

    fieldnames = ["test", "category", "polluters", "module", "strategy", "java",
                  "order_result", "alone_result", "observed", "verdict",
                  "label_agrees", "note"]
    results = []

    def emit(row):
        results.append(row)
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(results)

    for (repo, sha, module, strategy, java), members in groups.items():
        print(f"\n=== {repo} @ {sha[:10]}  module={module}  strategy={strategy}  java={java} "
              f"({len(members)} test(s)) ===")

        if args.dry_run:
            for m in members:
                pol = [to_surefire(p) for p in m["polluters"]]
                vic = to_surefire(m["test"])
                print(f"  [{m['category']}] order: {pol + [vic]}   alone: [{vic}]")
            continue

        java_home = compile_all.discover_java_home(int(java), java_explicit.get(int(java)))
        if not java_home:
            for m in members:
                emit({**base_row(m, module, strategy, java), "verdict": "ERROR",
                      "note": f"Java {java} home not found"})
            continue

        work = work_root / hashlib.sha256(f"{repo}{sha}{module}{strategy}".encode()).hexdigest()[:16]
        try:
            checkout(repo, sha, cache_dir, work)
            compile_all.apply_strategy(work, strategy, "strip")   # patch POMs for https/snapshot
        except Exception as exc:  # noqa: BLE001
            for m in members:
                emit({**base_row(m, module, strategy, java), "verdict": "ERROR",
                      "note": f"checkout/patch failed: {exc}"})
            continue

        code, out = compile_project(work, module, java_home, maven_repo, args.compile_timeout)
        if code != 0:
            tail = " || ".join(l.strip() for l in out.splitlines()[-6:])
            for m in members:
                emit({**base_row(m, module, strategy, java), "verdict": "COMPILE_FAILED",
                      "note": tail[-400:]})
            continue

        for m in members:
            vic = to_surefire(m["test"])
            pol = [to_surefire(p) for p in m["polluters"]]
            order_ids = pol + [vic]

            order_res, _ = run_tests(work, module, order_ids, True, m["test"], java_home, maven_repo, args.test_timeout)
            alone_res, _ = run_tests(work, module, [vic], False, m["test"], java_home, maven_repo, args.test_timeout)

            # Classify from the OBSERVED behaviour instead of trusting the (sometimes
            # ambiguous) dataset label: plain "OD" doesn't say victim vs brittle.
            #   VICTIM  = order FAIL / alone PASS  (polluter breaks a good test)
            #   BRITTLE = order PASS / alone FAIL  (test needs the other to run first)
            observed, verdict = classify(order_res, alone_res)

            # Does the observed pattern agree with the dataset's category label?
            label = m["category"]
            if observed in ("VICTIM", "BRITTLE"):
                if label == "OD-Brit":
                    matches = "yes" if observed == "BRITTLE" else "no"
                elif label == "OD-Vic":
                    matches = "yes" if observed == "VICTIM" else "no"
                else:  # plain "OD" -> label is ambiguous, either direction is fine
                    matches = "n/a (plain OD)"
            else:
                matches = ""

            row = base_row(m, module, strategy, java)
            row.update(order_result=order_res, alone_result=alone_res,
                       observed=observed, verdict=verdict, label_agrees=matches)
            emit(row)
            print(f"  [{label:8}] {m['test']}")
            print(f"      order={order_res} alone={alone_res} -> {verdict} ({observed}, label_agrees={matches})")

    # Summary
    from collections import Counter
    counts = Counter(r["verdict"] for r in results)
    print("\n===== SUMMARY =====")
    for k, n in counts.most_common():
        print(f"  {k}: {n}")
    print(f"Results written to {args.out}")


def base_row(m, module, strategy, java):
    return {
        "test": m["test"], "category": m["category"],
        "polluters": " / ".join(m["polluters"]),
        "module": module, "strategy": strategy, "java": java,
        "order_result": "", "alone_result": "", "observed": "",
        "verdict": "", "label_agrees": "", "note": "",
    }


if __name__ == "__main__":
    main()
