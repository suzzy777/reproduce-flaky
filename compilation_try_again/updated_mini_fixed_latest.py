#!/usr/bin/env python3
"""
minimize_fixed_patch_reproflake.py

Minimize ReproFlake Fixed.patch files at unified-diff @@ hunk granularity
while reusing the EXISTING ReproFlake runner.sh and Docker infrastructure.

Run this script from the ReproFlake repository root (the directory containing
runner.sh and data/). In that normal case you do NOT need to pass a repository
path.

Example:
    python3 minimize_fixed_patch_reproflake.py \
        --csv test_config.csv \
        --only-container ormlitecore59309e5

What the script does for each selected CSV row:
  1. Locate/download data/<zip>.zip.
  2. Extract that ZIP ONCE into a content-addressed cache.
  3. Parse the ZIP's original Fixed.patch into @@ hunks.
  4. For each ddmin candidate:
       - restore a clean working subject from the cached ZIP contents;
       - replace Fixed.patch with only the selected hunks;
       - write a one-row ReproFlake test_config.csv with config=Fixed;
       - call bash runner.sh;
       - runner.sh chooses the correct ID/OD/TD/NIO/etc. script;
       - that existing script creates Fixed/, launches Docker, compiles, and runs;
       - read data/<result_container>/result/Fixed/summary.txt.
  5. Save a 1-minimal hunk subset as Minimal.patch.

Important correctness details:
  * The empty patch is used only as an informational flaky baseline. It may
    PASS or FAIL. It is never accepted as the minimal developer fix.
  * Candidate patches are dry-run applied before Docker. Invalid subsets are
    rejected immediately.
  * By default, Flaky/ and the ZIP's Maven cache are restored from the pristine
    extraction for EVERY oracle run. This avoids cross-candidate Maven state.
  * ZIP decompression and patch parsing are cached.
  * PASS/FAIL results for the same test + hunk subset + iteration count are cached.
  * runner.sh's original test_config.csv is restored when this program exits.

TD warning:
  ReproFlake's config=Fixed for TD runs the ordinary fixed test. It does not
  automatically inject the deterministic timing perturbation from
  FixedCodeChange.patch. Therefore TD minimization with this oracle can be weak:
  the empty patch may pass because the original TD failure was not triggered.
  The script records and prints a warning for TD cases.
"""

import argparse
import csv
import hashlib
import json
import os
import pty
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ORACLE_VERSION = "reproflake-id-hunk-v6-baseline-informational"

HUNK_RE = re.compile(r"^@@")
DIFF_RE = re.compile(r"^diff\s")
SUMMARY_RE = re.compile(r"^(Passes|Failures|Errors):[ \t]*(\d*)[ \t]*\r?$", re.M)

RUNNER_FIELDS = [
    "test_type",
    "result_container",
    "zip",
    "module",
    "polluter/state setter",
    "flaky_test",
    "iterations",
    "config",
    "java",
    "nondexSeed",
    "url",
]


# ---------------------------------------------------------------------------
# General utilities
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_slug(s: str, max_len: int = 140) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(s))
    if len(s) <= max_len:
        return s
    digest = hashlib.sha1(s.encode()).hexdigest()[:10]
    return s[: max_len - 11] + "_" + digest


def run_logged(cmd, cwd: Path, env: dict, log_path: Path) -> int:
    """
    Run ReproFlake under a pseudo-terminal.

    ReproFlake's category scripts use commands such as:
        docker exec -it ...

    The -t option requires a TTY. Redirecting runner.sh directly to a normal
    file/pipe can therefore make Docker fail even though the outer shell script
    may eventually return 0. A PTY makes this behave like running
    `bash runner.sh` manually in a terminal while still saving a log.
    """
    print("      $", " ".join(map(str, cmd)))

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [str(x) for x in cmd],
            cwd=str(cwd),
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )

        os.close(slave_fd)
        slave_fd = None

        with log_path.open("wb") as log:
            while True:
                try:
                    chunk = os.read(master_fd, 8192)
                except OSError:
                    # PTYs commonly raise EIO when the child closes the slave.
                    break

                if not chunk:
                    break

                log.write(chunk)
                log.flush()

                # Also show the real ReproFlake/Docker output live.
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except Exception:
                    pass

        return proc.wait()

    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except OSError:
                pass



def detect_reproflake_root(explicit: Path | None) -> Path:
    """
    Normal use: run this script from the ReproFlake repo root.
    Fallback: if the script itself was copied into that root, detect its parent.
    """
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.append(Path.cwd())
    candidates.append(Path(__file__).resolve().parent)

    seen = set()
    for c in candidates:
        c = c.resolve()
        if c in seen:
            continue
        seen.add(c)
        if (c / "runner.sh").is_file() and (c / "data").is_dir():
            return c

    raise SystemExit(
        "Could not locate the ReproFlake repository root. "
        "Run this script from the directory containing runner.sh and data/, "
        "or pass --reproflake-dir /path/to/ReproFlake."
    )


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def load_rows(csv_path: Path):
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        rows = list(reader)

    required = {"test_type", "result_container", "zip", "module", "flaky_test"}
    missing = required - set(fields)
    if missing:
        raise SystemExit(
            f"Input CSV is missing required ReproFlake columns: {sorted(missing)}"
        )
    return fields, rows


def write_runner_csv(path: Path, row: dict, iterations: int, result_container_override: str | None = None):
    """
    runner.sh reads fields by POSITION, so emit exactly the 11 columns it expects.
    """
    out = {k: row.get(k, "") or "" for k in RUNNER_FIELDS}
    out["iterations"] = str(iterations)
    out["config"] = "Fixed"
    if result_container_override is not None:
        out["result_container"] = result_container_override

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RUNNER_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerow(out)


# ---------------------------------------------------------------------------
# ZIP locate/download/extraction cache
# ---------------------------------------------------------------------------

def locate_zip(repo_root: Path, zip_value: str) -> Path:
    z = zip_value.strip()
    if z.endswith(".zip"):
        return repo_root / "data" / z
    return repo_root / "data" / f"{z}.zip"


def ensure_zip_available(repo_root: Path, row: dict) -> Path:
    zip_path = locate_zip(repo_root, row["zip"])
    if zip_path.is_file():
        return zip_path

    url = (row.get("url") or "").strip()
    if not url:
        raise RuntimeError(f"Missing ZIP {zip_path} and CSV row has no URL.")

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    part = zip_path.with_suffix(zip_path.suffix + ".part")

    print(f"    downloading {url}")
    try:
        urllib.request.urlretrieve(url, part)
        part.replace(zip_path)
    except Exception:
        try:
            part.unlink()
        except FileNotFoundError:
            pass
        raise

    return zip_path


def find_subject_root(extract_root: Path) -> Path:
    candidates = []
    for p in extract_root.rglob("Fixed.patch"):
        parent = p.parent
        if (parent / "Flaky").is_dir():
            candidates.append(parent)

    if not candidates:
        raise RuntimeError(
            f"Could not find a directory containing both Fixed.patch and Flaky under {extract_root}"
        )

    candidates.sort(key=lambda p: len(p.relative_to(extract_root).parts))
    return candidates[0]


def ensure_zip_cache(zip_path: Path, cache_dir: Path) -> tuple[Path, str]:
    """
    Decompress each unique ZIP only once. The returned subject directory is
    PRISTINE and must never be modified.
    """
    zip_hash = sha256_file(zip_path)
    entry = cache_dir / "zip" / zip_hash
    subject = entry / "subject"
    marker = entry / "complete.json"

    if (
        marker.is_file()
        and (subject / "Fixed.patch").is_file()
        and (subject / "Flaky").is_dir()
    ):
        print(f"    ZIP cache HIT  {zip_hash[:12]}  {zip_path.name}")
        return subject, zip_hash

    print(f"    ZIP cache MISS {zip_hash[:12]}  extracting {zip_path.name}")

    tmp = entry.with_name(entry.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp / "raw")

    root = find_subject_root(tmp / "raw")
    shutil.copytree(root, tmp / "subject", symlinks=True)

    (tmp / "complete.json").write_text(
        json.dumps(
            {
                "zip": str(zip_path),
                "sha256": zip_hash,
                "subject_root_in_zip": str(root.relative_to(tmp / "raw")),
            },
            indent=2,
        )
    )

    entry.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(entry, ignore_errors=True)
    tmp.rename(entry)
    return entry / "subject", zip_hash


def copy_tree_fast(src: Path, dst: Path):
    """
    Prefer copy-on-write reflinks when the filesystem supports them.
    Falls back to an ordinary recursive copy.
    """
    shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)

    cp = shutil.which("cp")
    if cp:
        p = subprocess.run(
            [cp, "-a", "--reflink=auto", f"{src}/.", str(dst)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if p.returncode == 0:
            return

    shutil.copytree(src, dst, symlinks=True)


def restore_clean_workspace(
    pristine_subject: Path,
    workspace: Path,
    candidate_patch: str,
):
    """
    Restore the exact ZIP baseline before EACH oracle run.

    This is intentionally cleaner than preserving .m2 between candidates.
    The framework normally extracts the ZIP anew each run; this reproduces that
    isolation without paying ZIP decompression every time.
    """
    copy_tree_fast(pristine_subject, workspace)

    # Old result artifacts in the archive are not part of the oracle.
    shutil.rmtree(workspace / "result", ignore_errors=True)
    shutil.rmtree(workspace / "Fixed", ignore_errors=True)

    (workspace / "Fixed.patch").write_text(candidate_patch, encoding="utf-8")


# ---------------------------------------------------------------------------
# Patch parsing/cache
# ---------------------------------------------------------------------------

def parse_patch_text(text: str):
    lines = text.splitlines(keepends=True)

    files = []
    current_file = None
    current_hunk = None
    next_id = 0

    def flush_hunk():
        nonlocal current_hunk
        if current_file is not None and current_hunk is not None:
            current_file["hunks"].append(current_hunk)
            current_hunk = None

    def flush_file():
        nonlocal current_file
        if current_file is not None:
            flush_hunk()
            files.append(current_file)
            current_file = None

    for line in lines:
        if DIFF_RE.match(line):
            flush_file()
            current_file = {"header": [line], "hunks": []}
            continue

        if current_file is None:
            current_file = {"header": [], "hunks": []}

        if HUNK_RE.match(line):
            flush_hunk()
            current_hunk = {
                "id": next_id,
                "header": line.rstrip("\n"),
                "lines": [line],
            }
            next_id += 1
            continue

        if current_hunk is not None:
            current_hunk["lines"].append(line)
        else:
            current_file["header"].append(line)

    flush_file()
    return files


def ensure_patch_cache(original_patch: bytes, cache_dir: Path):
    patch_hash = sha256_bytes(original_patch)
    cache_file = cache_dir / "patch" / f"{patch_hash}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    if cache_file.is_file():
        print(f"    patch cache HIT  {patch_hash[:12]}")
        return json.loads(cache_file.read_text()), patch_hash

    print(f"    patch cache MISS {patch_hash[:12]}  parsing @@ hunks")
    files = parse_patch_text(original_patch.decode("utf-8", errors="replace"))
    cache_file.write_text(json.dumps(files))
    return files, patch_hash


def all_hunks(files):
    return [h["id"] for f in files for h in f["hunks"]]


def file_name(header):
    for line in header:
        if line.startswith("--- "):
            return line[4:].split("\t", 1)[0].strip()
    for line in header:
        if line.startswith("diff "):
            return line.strip()
    return "unknown"


def build_patch(files, selected):
    selected = set(selected)
    out = []

    for f in files:
        kept = [h for h in f["hunks"] if h["id"] in selected]
        if not kept:
            continue
        out.extend(f["header"])
        for h in kept:
            out.extend(h["lines"])

    return "".join(out)


def patch_dry_run(flaky_dir: Path, patch_text: str) -> tuple[bool, str]:
    """
    Check that the selected hunks can be applied as a set before paying for Docker.
    Empty patch is valid and means Fixed == Flaky.
    """
    if not patch_text.strip():
        return True, "empty patch"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(patch_text)
        temp_patch = Path(tf.name)

    try:
        p = subprocess.run(
            [
                "patch",
                "--dry-run",
                "--batch",
                "-p1",
                "-d",
                str(flaky_dir),
                "-i",
                str(temp_patch),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return p.returncode == 0, p.stdout
    finally:
        try:
            temp_patch.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Only shim needed: skip repeated unzip
# ---------------------------------------------------------------------------

def create_unzip_shim(root: Path) -> Path:
    """
    Category scripts always call unzip -o data/<zip>.zip into data/<result_container>.
    We already restored that workspace from the pristine extraction cache, so
    skip ONLY that redundant unzip call.

    rm is NOT shimmed: the framework may clean Fixed/ and .m2 normally.
    """
    bindir = root / "bin"
    bindir.mkdir(parents=True, exist_ok=True)

    real_unzip = shutil.which("unzip") or "/usr/bin/unzip"
    shim = bindir / "unzip"
    shim.write_text(
        f"""#!/bin/bash
if [[ "${{REPROFLAKE_DDMIN_PREPARED:-0}}" == "1" ]]; then
    echo "[ddmin] skip redundant unzip: $*" >&2
    exit 0
fi
exec {real_unzip!r} "$@"
"""
    )
    shim.chmod(0o755)
    return bindir


def print_missing_summary_diagnostics(
    workspace: Path,
    summary_path: Path,
    log_path: Path,
    runner_rc: int,
    tail_lines: int = 100,
):
    """
    Missing summary means the ReproFlake run did not reach a normal test result.
    Print enough state to distinguish Docker/build/NonDex/result-copy failures.
    """
    print()
    print("      !!! ReproFlake did not produce the expected summary")
    print(f"      runner exit code: {runner_rc}")
    print(f"      expected summary: {summary_path}")
    print(f"      workspace:        {workspace}")
    print(f"      runner log:       {log_path}")

    # Show any summary files that did get created elsewhere.
    found_summaries = []
    if workspace.exists():
        try:
            found_summaries = list(workspace.rglob("summary.txt"))
        except Exception:
            found_summaries = []

    if found_summaries:
        print("      summary.txt files found elsewhere:")
        for p in found_summaries[:20]:
            print(f"        - {p}")
    else:
        print("      no summary.txt found anywhere under the workspace")

    # Compact view of result/flaky-result state.
    interesting = [
        workspace / "result",
        workspace / "Fixed",
        workspace / "Flaky",
        workspace / "Fixed" / "flaky-result",
        workspace / "Flaky" / "flaky-result",
    ]
    print("      important paths:")
    for p in interesting:
        if p.exists():
            kind = "dir" if p.is_dir() else "file"
            print(f"        EXISTS [{kind}] {p}")
        else:
            print(f"        MISSING       {p}")

    # Show shallow result contents if present.
    result_dir = workspace / "result"
    if result_dir.exists():
        print("      result/ contents:")
        try:
            entries = sorted(result_dir.rglob("*"))
            for p in entries[:80]:
                rel = p.relative_to(workspace)
                suffix = "/" if p.is_dir() else ""
                print(f"        {rel}{suffix}")
            if len(entries) > 80:
                print(f"        ... ({len(entries) - 80} more)")
        except Exception as e:
            print(f"        <could not list result/: {e}>")

    print()
    print(f"      ---- last {tail_lines} lines of runner log ----")
    try:
        raw = log_path.read_bytes()
        decoded = raw.decode("utf-8", errors="replace")
        lines = decoded.splitlines()
        for line in lines[-tail_lines:]:
            print("      | " + line)
    except Exception as e:
        print(f"      | <could not read log: {e}>")
    print("      ---- end runner log tail ----")
    print()


# ---------------------------------------------------------------------------
# Result oracle
# ---------------------------------------------------------------------------

def read_fixed_summary(workspace: Path):
    """
    Read ReproFlake's flaky-result/summary.txt.

    The existing ID statistics scripts do not initialize pass_count,
    fail_count, and error_count. Therefore a zero count may be written as
    a blank value, for example:

        Summary:
        Passes:
        Failures: 1
        Errors:

    Treat a blank count as 0.
    """
    summary = workspace / "result" / "Fixed" / "summary.txt"
    if not summary.is_file():
        return None, summary

    text = summary.read_text(encoding="utf-8", errors="replace")

    vals = {}
    for m in SUMMARY_RE.finditer(text):
        raw = m.group(2).strip()
        vals[m.group(1)] = int(raw) if raw else 0

    if not {"Passes", "Failures", "Errors"} <= set(vals):
        print("      summary.txt exists but could not be parsed completely")
        print("      ---- summary.txt ----")
        for line in text.splitlines():
            print("      | " + line)
        print("      ---- end summary.txt ----")
        return None, summary

    print(
        f"      parsed summary: "
        f"Passes={vals['Passes']} "
        f"Failures={vals['Failures']} "
        f"Errors={vals['Errors']}"
    )
    return vals, summary



def summary_is_pass(vals, requested_iterations: int) -> bool:
    if vals is None:
        return False
    return (
        vals["Failures"] == 0
        and vals["Errors"] == 0
        and vals["Passes"] >= requested_iterations
    )


# ---------------------------------------------------------------------------
# ddmin
# ---------------------------------------------------------------------------

def split_chunks(items, n):
    items = list(items)
    n = max(1, min(n, len(items)))
    q, r = divmod(len(items), n)
    chunks = []
    start = 0
    for i in range(n):
        size = q + (1 if i < r else 0)
        if size:
            chunks.append(items[start:start + size])
            start += size
    return chunks


def ddmin(changes, oracle):
    """
    Find a 1-minimal NON-EMPTY subset at hunk granularity.

    The empty patch is checked before ddmin as a reproduction sanity check:
    the original Flaky source must fail. Therefore an empty patch is never
    accepted as the minimal developer fix.
    """
    changes = list(changes)

    if len(changes) <= 1:
        return changes

    n = 2
    while len(changes) >= 2:
        subsets = split_chunks(changes, n)
        reduced = False

        for subset in subsets:
            print(f"    try subset      {subset}")
            if oracle(subset):
                print("      PASS -> reduce to subset")
                changes = list(subset)
                n = max(2, n - 1)
                reduced = True
                break
            print("      FAIL")

        if reduced:
            continue

        for subset in subsets:
            rem = set(subset)
            complement = [x for x in changes if x not in rem]

            # Empty was already tested above.
            if not complement:
                continue

            print(f"    try complement  {complement}  (remove {subset})")
            if oracle(complement):
                print("      PASS -> removed subset")
                changes = complement
                n = max(2, n - 1)
                reduced = True
                break
            print("      FAIL")

        if reduced:
            continue

        if n >= len(changes):
            break
        n = min(len(changes), n * 2)

    # Explicit 1-minimal deletion sweep.
    changed = True
    while changed and changes:
        changed = False
        for h in list(changes):
            candidate = [x for x in changes if x != h]

            # Empty patch is only a baseline sanity check, not a valid fix.
            if not candidate:
                continue

            print(f"    final sweep remove H{h}: {candidate}")
            if oracle(candidate):
                print(f"      PASS -> remove H{h}")
                changes = candidate
                changed = True
                break
            print(f"      FAIL -> keep H{h}")

    return changes


# ---------------------------------------------------------------------------
# One test
# ---------------------------------------------------------------------------

def minimize_test(
    row: dict,
    repo_root: Path,
    cache_dir: Path,
    output_root: Path,
    search_iterations: int,
    final_iterations: int,
    shim_dir: Path,
    list_hunks_only: bool,
):
    original_rc = row["result_container"].strip()
    zip_value = row["zip"].strip()
    test = row["flaky_test"].strip()
    typ = row["test_type"].strip().lower()

    zip_path = ensure_zip_available(repo_root, row)
    pristine_subject, zip_hash = ensure_zip_cache(zip_path, cache_dir)
    pristine_patch = (pristine_subject / "Fixed.patch").read_bytes()

    files, patch_hash = ensure_patch_cache(pristine_patch, cache_dir)
    hunks = all_hunks(files)
    if not hunks:
        raise RuntimeError("Fixed.patch contains no @@ hunks.")

    print()
    print("=" * 92)
    print(f"TEST:      {test}")
    print(f"TYPE:      {typ}")
    print(f"ZIP:       {zip_value}")
    print(f"CONTAINER: {original_rc}")
    print(f"HUNKS:     {len(hunks)}")
    for f in files:
        fn = file_name(f["header"])
        for h in f["hunks"]:
            print(f"  H{h['id']:03d}  {fn}  {h['header']}")
    print("=" * 92)

    if typ == "td":
        print(
            "\n  WARNING [TD]: runner config=Fixed does not apply the deterministic "
            "FixedCodeChange timing perturbation. A reduced or empty patch can pass "
            "simply because the timing failure was not triggered."
        )

    if list_hunks_only:
        return {
            "result_container": original_rc,
            "zip": zip_value,
            "flaky_test": test,
            "test_type": typ,
            "original_hunks": hunks,
            "listed_only": True,
        }

    # Never overwrite the user's normal data/<result_container> results.
    # Use a short synthetic ReproFlake working/container name for this test.
    work_id = "ddmin_" + hashlib.sha1(
        (original_rc + "\n" + test + "\n" + patch_hash).encode("utf-8")
    ).hexdigest()[:16]
    workspace = repo_root / "data" / work_id

    out_dir = output_root / safe_slug(original_rc) / safe_slug(test)
    logs = out_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    # Cache key includes every runner-relevant field, not only test name.
    runner_metadata = {k: row.get(k, "") or "" for k in RUNNER_FIELDS}
    oracle_identity = {
        "oracle_version": ORACLE_VERSION,
        "patch_sha256": patch_hash,
        "runner_metadata": runner_metadata,
    }
    oracle_key = sha256_bytes(
        json.dumps(oracle_identity, sort_keys=True).encode("utf-8")
    )
    oracle_file = cache_dir / "oracle" / f"{oracle_key}.json"
    oracle_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        oracle_cache = (
            json.loads(oracle_file.read_text()) if oracle_file.is_file() else {}
        )
    except Exception:
        oracle_cache = {}

    eval_no = 0
    framework_csv = repo_root / "test_config.csv"
    runner = repo_root / "runner.sh"
    if not runner.is_file():
        raise RuntimeError(f"Missing runner.sh at {runner}")

    def save_oracle():
        oracle_file.write_text(json.dumps(oracle_cache, indent=2, sort_keys=True))

    def oracle(selected, iterations=None, force=False, require_summary=False):
        nonlocal eval_no

        selected = tuple(sorted(selected))
        iterations = search_iterations if iterations is None else iterations
        cache_key = f"{iterations}:" + ",".join(map(str, selected))

        if not force and cache_key in oracle_cache:
            ok = bool(oracle_cache[cache_key]["passed"])
            print(f"      oracle cache -> {'PASS' if ok else 'FAIL'}")
            return ok

        candidate_text = build_patch(files, selected)

        # Reject an internally inconsistent hunk subset before Docker.
        apply_ok, apply_output = patch_dry_run(
            pristine_subject / "Flaky", candidate_text
        )
        if not apply_ok:
            oracle_cache[cache_key] = {
                "passed": False,
                "reason": "patch-dry-run-failed",
                "selected_hunks": list(selected),
                "iterations": iterations,
                "patch_output": apply_output[-4000:],
            }
            save_oracle()
            print("      => FAIL (candidate hunks do not apply cleanly)")
            return False

        eval_no += 1

        # Restore Flaky + Maven cache from the pristine extracted ZIP baseline.
        restore_clean_workspace(pristine_subject, workspace, candidate_text)

        # runner.sh is hard-coded to read <repo>/test_config.csv.
        write_runner_csv(
            framework_csv, row, iterations, result_container_override=work_id
        )

        env = os.environ.copy()
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
        env["REPROFLAKE_DDMIN_PREPARED"] = "1"

        candidate_copy = out_dir / f"candidate_{eval_no:04d}.patch"
        candidate_copy.write_text(candidate_text, encoding="utf-8")

        log = logs / f"candidate_{eval_no:04d}.log"

        print(f"      selected hunks:   {list(selected)}")
        print(f"      iterations:       {iterations}")
        print(f"      work container:   {work_id}")
        print(f"      workspace:        {workspace}")
        print(f"      candidate patch:  {candidate_copy}")
        print(f"      runner log:       {log}")
        print("      ---- live runner output ----")

        t0 = time.time()
        runner_rc = run_logged(["bash", "runner.sh"], repo_root, env, log)

        print("      ---- runner finished ----")
        elapsed = time.time() - t0

        vals, summary_path = read_fixed_summary(workspace)
        # summary.txt is the actual per-test result oracle. runner_rc is recorded
        # separately because some framework scripts do not consistently propagate
        # inner Maven/Docker exit codes.
        passed = summary_is_pass(vals, iterations)

        oracle_cache[cache_key] = {
            "passed": passed,
            "runner_exit_code": runner_rc,
            "iterations": iterations,
            "selected_hunks": list(selected),
            "summary": vals,
            "summary_path": str(summary_path),
            "elapsed_seconds": round(elapsed, 3),
            "log": str(log),
            "candidate_patch": str(candidate_copy),
        }
        save_oracle()

        if vals is None:
            print(
                f"      => INFRASTRUCTURE ERROR "
                f"(no valid result/Fixed/summary.txt; runner rc={runner_rc})"
            )
            print_missing_summary_diagnostics(
                workspace=workspace,
                summary_path=summary_path,
                log_path=log,
                runner_rc=runner_rc,
            )

            if require_summary:
                raise RuntimeError(
                    "ReproFlake did not produce result/Fixed/summary.txt. "
                    "This is not a valid PASS/FAIL oracle result. "
                    f"Inspect: {log}"
                )

            # During ddmin, a candidate that cannot build/run is insufficient.
            return False

        print(
            f"      => {'PASS' if passed else 'FAIL'} "
            f"(P={vals['Passes']} F={vals['Failures']} E={vals['Errors']}, "
            f"{elapsed:.1f}s, runner rc={runner_rc})"
        )
        return passed

    print("\n  Baseline check 1/2: original Flaky source (no patch)")
    baseline_passed = oracle([], force=True, require_summary=True)
    if baseline_passed:
        print(
            "      NOTE: the unpatched flaky test PASSED in this run. "
            "That is allowed for a flaky test, so minimization will continue."
        )
    else:
        print(
            "      The unpatched flaky test FAILED in this run, "
            "which confirms the flaky behavior was triggered this time."
        )

    print("\n  Required check 2/2: complete developer Fixed.patch must PASS")
    if not oracle(hunks, force=True, require_summary=True):
        raise RuntimeError(
            "The full developer Fixed.patch did not pass the Fixed-version oracle. "
            "Inspect the candidate log before minimizing."
        )

    print("\n  Starting ddmin...")
    minimal = ddmin(hunks, oracle)

    print(f"\n  Final confirmation: {final_iterations} iteration(s)")
    if not oracle(minimal, iterations=final_iterations, force=True):
        raise RuntimeError(
            "The search-minimized patch failed final confirmation. "
            "Increase --search-iterations or inspect the final candidate log."
        )

    minimal_text = build_patch(files, minimal)
    minimal_path = out_dir / "Minimal.patch"
    minimal_path.write_text(minimal_text, encoding="utf-8")

    result = {
        "result_container": original_rc,
        "ddmin_work_container": work_id,
        "zip": zip_value,
        "flaky_test": test,
        "test_type": typ,
        "patch_sha256": patch_hash,
        "zip_sha256": zip_hash,
        "original_hunks": hunks,
        "minimal_hunks": minimal,
        "original_hunk_count": len(hunks),
        "minimal_hunk_count": len(minimal),
        "search_iterations": search_iterations,
        "final_iterations": final_iterations,
        "oracle_evaluations_this_run": eval_no,
        "minimal_patch": str(minimal_path),
        "td_oracle_warning": typ == "td",
    }
    (out_dir / "result.json").write_text(json.dumps(result, indent=2))

    # The synthetic ddmin workspace is disposable; do not touch the user's
    # original data/<result_container> folder.
    shutil.rmtree(workspace, ignore_errors=True)

    print(f"\n  DONE: {len(hunks)} -> {len(minimal)} hunks")
    print(f"  {minimal_path}")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=(
            "Minimize ReproFlake Fixed.patch using the existing runner.sh "
            "and Docker/category scripts."
        )
    )

    ap.add_argument(
        "--reproflake-dir",
        type=Path,
        default=None,
        help=(
            "ReproFlake repo root. Normally omit this and run the script from "
            "the directory containing runner.sh and data/."
        ),
    )
    ap.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Input test-config CSV. Default: <ReproFlake repo>/test_config.csv",
    )
    ap.add_argument(
        "--search-iterations",
        type=int,
        default=5,
        help="Fixed runs per ddmin candidate (default: 5).",
    )
    ap.add_argument(
        "--final-iterations",
        type=int,
        default=10,
        help="Fixed runs for final confirmation (default: 10).",
    )
    ap.add_argument("--row-index", type=int, default=None)
    ap.add_argument(
        "--only-type",
        default=None,
        help="Process only this flaky-test type, e.g. id, od, td, nio.",
    )
    ap.add_argument("--only-container", default=None)
    ap.add_argument("--only-test", default=None)
    ap.add_argument(
        "--list-hunks",
        action="store_true",
        help="Only show hunk decomposition; do not run Docker/ddmin.",
    )
    ap.add_argument(
        "--output-root",
        type=Path,
        default=Path("minimal_patch_results"),
    )
    ap.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".ddmin_cache"),
    )

    args = ap.parse_args()

    repo_root = detect_reproflake_root(args.reproflake_dir)

    csv_path = (
        args.csv.resolve()
        if args.csv is not None
        else (repo_root / "test_config.csv").resolve()
    )

    # Relative output/cache locations live under the ReproFlake repo for
    # predictable behavior regardless of where the Python file itself is stored.
    output_root = (
        args.output_root.resolve()
        if args.output_root.is_absolute()
        else (repo_root / args.output_root).resolve()
    )
    cache_dir = (
        args.cache_dir.resolve()
        if args.cache_dir.is_absolute()
        else (repo_root / args.cache_dir).resolve()
    )

    _, rows = load_rows(csv_path)

    selected = []
    for i, row in enumerate(rows):
        if args.row_index is not None and i != args.row_index:
            continue
        if (
            args.only_type is not None
            and row["test_type"].strip().lower() != args.only_type.strip().lower()
        ):
            continue
        if (
            args.only_container is not None
            and row["result_container"] != args.only_container
        ):
            continue
        if args.only_test is not None and row["flaky_test"] != args.only_test:
            continue
        selected.append((i, row))

    if not selected:
        raise SystemExit("No matching CSV rows.")

    framework_csv = repo_root / "test_config.csv"
    original_framework_csv = (
        framework_csv.read_bytes() if framework_csv.exists() else None
    )

    shim_root = Path(tempfile.mkdtemp(prefix="reproflake-ddmin-shim-"))
    shim_dir = create_unzip_shim(shim_root)

    results = []
    failures = []

    try:
        for idx, row in selected:
            print(f"\n### input CSV row {idx}")
            try:
                result = minimize_test(
                    row=row,
                    repo_root=repo_root,
                    cache_dir=cache_dir,
                    output_root=output_root,
                    search_iterations=args.search_iterations,
                    final_iterations=args.final_iterations,
                    shim_dir=shim_dir,
                    list_hunks_only=args.list_hunks,
                )
                result["csv_row_index"] = idx
                results.append(result)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[ERROR] row {idx}: {e}", file=sys.stderr)
                failures.append(
                    {
                        "csv_row_index": idx,
                        "result_container": row.get("result_container", ""),
                        "flaky_test": row.get("flaky_test", ""),
                        "error": str(e),
                    }
                )
    finally:
        shutil.rmtree(shim_root, ignore_errors=True)

        # Put the framework's original test_config.csv back exactly as it was.
        if original_framework_csv is None:
            try:
                framework_csv.unlink()
            except FileNotFoundError:
                pass
        else:
            framework_csv.write_bytes(original_framework_csv)

    output_root.mkdir(parents=True, exist_ok=True)
    summary = {
        "successful": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
    (output_root / "summary.json").write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 92)
    print(f"Successful: {len(results)}")
    print(f"Failed:     {len(failures)}")
    print(f"Summary:    {output_root / 'summary.json'}")

    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
