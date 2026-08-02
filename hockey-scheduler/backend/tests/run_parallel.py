#!/usr/bin/env python3
"""Parallel test runner — shard test modules across processes to cut wall-clock.

The suite is ~2000 tests and runs serially under ``python -m unittest`` in
5-10 min. This shards the test MODULES across worker processes (isolated
interpreters, so the module-global ``srv.STATE``/``SESSIONS`` singletons and
``os.environ`` don't collide) and runs each shard as its own
``python -m unittest`` invocation.

Usage (from ``backend/tests``):

    python3 run_parallel.py                 # Memory + SQLite(:memory:), N=cpu-1
    python3 run_parallel.py -j 4            # explicit worker count
    python3 run_parallel.py --postgres URL  # PostgreSQL; each worker gets its
                                            # OWN database (URL_p0, URL_p1, …)
                                            # so concurrent workers never share
                                            # one Postgres DB's tables.

Exit code is non-zero if any shard fails; failing shards' output is printed.
HS_PBKDF2_ITERATIONS is already lowered by tests/helpers.py at import time.

Diagnosability (#382 postmortem): a failing shard's report used to be a blind
tail slice (``err[-4000:]``) of raw output. The HTTP tests leak listening
sockets whose ``ResourceWarning: unclosed <socket …>`` lines are emitted by the
garbage collector at INTERPRETER SHUTDOWN — i.e. *after* unittest has printed
``FAILED (failures=1)`` — so hundreds of them sat in the last 4000 characters
and pushed the actual ``FAIL:``/``Traceback``/``AssertionError`` block out of
the window. Red CI carried no assertion at all. Both halves are fixed here:
the noise is filtered out of the captured TEXT (never suppressed in the child,
so a genuine leak regression is still observable), and what is printed is
anchored to the failure section rather than to a byte offset.
"""

import argparse
import concurrent.futures
import glob
import os
import re
import subprocess
import sys
import time

# ``<sys>:0: ResourceWarning: unclosed <socket.socket fd=3, …>`` — and the
# attributed ``/path/to/file.py:484: ResourceWarning: unclosed <ssl.SSLSocket …>``
# form. Deliberately narrow: only *unclosed socket* ResourceWarnings are noise.
# Any other warning (including other ResourceWarnings — unclosed files, unclosed
# database connections) is real signal about a leak and is kept.
_SOCKET_WARNING_RE = re.compile(
    r"^\S.*?:\d+: ResourceWarning: unclosed <(?:socket|ssl)\b")
_TRACEMALLOC_HINT = (
    "ResourceWarning: Enable tracemalloc to get the object allocation traceback")

# unittest's failure report opens with a ``===`` rule, then ``FAIL:``/``ERROR:``.
_FAILURE_MARKER_RE = re.compile(r"^(?:FAIL|ERROR): \S")
_TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\):")
_RULE_RE = re.compile(r"^=+$")


def _strip_socket_warnings(text):
    """Drop ``unclosed <socket …>`` ResourceWarnings from CAPTURED TEXT.

    Filtering the text rather than setting ``PYTHONWARNINGS=ignore::ResourceWarning``
    on the shard subprocess is the deliberate choice: the env var changes the
    child interpreter's warning state, which (a) would equally hide unclosed
    *file* and unclosed *database connection* warnings — the ones that are a
    real regression signal — and (b) is a behaviour change to the code under
    test, not to the report. Filtering here cannot alter a single test outcome.

    Python renders a warning as a header line optionally followed by one
    indented echo of the source line and the tracemalloc hint; all three are
    dropped together. Returns ``(text, lines_dropped)``.
    """
    kept = []
    dropped = 0
    just_dropped_header = False
    for line in text.splitlines(True):
        if _SOCKET_WARNING_RE.match(line):
            dropped += 1
            just_dropped_header = True
            continue
        if just_dropped_header:
            just_dropped_header = False
            # The echoed source line is indented; the hint is its own line.
            # A traceback's first line is unindented, so it is never eaten.
            if (line[:1].isspace() and line.strip()) or line.strip() == _TRACEMALLOC_HINT:
                dropped += 1
                just_dropped_header = True
                continue
        kept.append(line)
    return "".join(kept), dropped


def _summary_line(text):
    """Last line that actually says something — ``OK`` / ``FAILED (…)`` / an error."""
    for line in reversed(text.strip().splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _failure_excerpt(text, max_lines):
    """Return the report anchored to the FAILURE SECTION, not to a byte offset.

    Starts at the first ``FAIL:``/``ERROR:`` banner (including its ``===`` rule)
    or, failing that, the first traceback, so the failing test name, its
    file:line and its assertion are always inside the window. ``max_lines <= 0``
    means print everything. If the failure section itself overflows the budget,
    the HEAD (first failures, with their assertions) and the TAIL (``Ran N
    tests`` / ``FAILED (…)``) are both kept and the middle is elided — never the
    other way round.
    """
    lines = text.splitlines()
    if max_lines <= 0 or len(lines) <= max_lines:
        return "\n".join(lines)          # nothing to bound; keep every line
    start = 0
    for i, line in enumerate(lines):
        if _FAILURE_MARKER_RE.match(line):
            start = i - 1 if i and _RULE_RE.match(lines[i - 1]) else i
            break
        if _TRACEBACK_RE.match(line):
            start = i
            break
    head_note = []
    if start:
        head_note = [f"[… {start} line(s) before the first failure omitted …]"]
    body = lines[start:]
    if max_lines > 0 and len(body) > max_lines:
        head_n = max(1, (max_lines * 2) // 3)
        tail_n = max(1, max_lines - head_n)
        body = (body[:head_n]
                + [f"[… {len(body) - head_n - tail_n} line(s) elided from the "
                   f"middle of the failure section …]"]
                + body[-tail_n:])
    return "\n".join(head_note + body)


def _failing_test_names(text):
    """``FAIL: test_x (mod.Class.test_x)`` → ``mod.Class.test_x`` roll-up."""
    names = []
    for line in text.splitlines():
        m = re.match(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)", line)
        if m:
            names.append(f"{m.group(1)}: {m.group(3)}")
        elif re.match(r"^(FAIL|ERROR): \S+$", line):
            names.append(line.strip())
    return names


def _shards(modules, n):
    """Round-robin split so the heavy HTTP-heavy modules spread across workers."""
    buckets = [[] for _ in range(n)]
    for i, m in enumerate(sorted(modules)):
        buckets[i % n].append(m)
    return [b for b in buckets if b]


def _pg_db_url(base_url, suffix):
    """Return ``base_url`` with the database name suffixed (…/hockey → …/hockey_p0)."""
    head, _, db = base_url.rpartition("/")
    return f"{head}/{db}{suffix}"


def _ensure_pg_database(base_url, dbname):
    import psycopg
    admin = _pg_db_url(base_url, "").rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{dbname}"')


def _run_shard(args):
    shard, env = args
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "-q", *shard],
        capture_output=True, text=True, env=env)
    return shard, proc.returncode, proc.stdout, proc.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--postgres", metavar="BASE_URL", default=None,
                    help="run against PostgreSQL, one DB per worker")
    ap.add_argument("--fail-output-lines", type=int, default=0, metavar="N",
                    help="bound each failing shard's report to N lines, keeping "
                         "the failure section's head and tail (default 0 = "
                         "print the whole thing)")
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    modules = [os.path.basename(p)[:-3] for p in glob.glob("test_*.py")]
    n = min(args.jobs, len(modules)) or 1
    shards = _shards(modules, n)

    jobs = []
    for i, shard in enumerate(shards):
        env = dict(os.environ)
        if args.postgres:
            dbname = args.postgres.rstrip("/").rsplit("/", 1)[-1] + f"_p{i}"
            _ensure_pg_database(args.postgres, dbname)
            env["TEST_DATABASE_URL"] = _pg_db_url(args.postgres, f"_p{i}")
        jobs.append((shard, env))

    backend = "PostgreSQL" if args.postgres else "Memory/SQLite"
    print(f"Running {len(modules)} test modules across {n} workers ({backend})…")
    t0 = time.time()
    failed = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=n) as ex:
        for i, (shard, code, out, err) in enumerate(ex.map(_run_shard, jobs)):
            # unittest reports on stderr; strip the shutdown-time socket noise
            # BEFORE picking the summary line, or the status column reads
            # "ResourceWarning: unclosed <socket…>" instead of "FAILED (…)".
            err, err_dropped = _strip_socket_warnings(err)
            out, out_dropped = _strip_socket_warnings(out)
            status = "ok" if code == 0 else "FAIL"
            print(f"  [{status}] shard {i} ({len(shard)} modules)  "
                  f"{_summary_line(err) or _summary_line(out)}")
            if code != 0:
                failed.append((i, shard, code, out, err, err_dropped + out_dropped))

    elapsed = time.time() - t0
    if failed:
        print("\n===== FAILURES =====")
        for i, shard, code, out, err, dropped in failed:
            print(f"\n--- shard {i} (exit {code}), modules: {' '.join(shard)} ---")
            if dropped:
                print(f"[{dropped} 'ResourceWarning: unclosed <socket …>' line(s) "
                      f"filtered from this report; they are emitted at "
                      f"interpreter shutdown and are not the failure]")
            if err.strip():
                print(f"--- shard {i} stderr ---")
                print(_failure_excerpt(err, args.fail_output_lines))
            if out.strip():
                print(f"--- shard {i} stdout ---")
                print(_failure_excerpt(out, args.fail_output_lines))
        names = []
        for i, shard, code, out, err, dropped in failed:
            names += [f"  shard {i}: {nm}" for nm in _failing_test_names(err + out)]
        if names:
            print("\n===== FAILING TESTS =====")
            print("\n".join(names))
        else:
            print("\n===== FAILING TESTS =====\n  (none reported by unittest — a "
                  "shard died before/outside the test run; see its output above)")
        print(f"\nFAILED ({len(failed)} shard(s)) in {elapsed:.0f}s")
        return 1
    print(f"\nOK — all shards passed in {elapsed:.0f}s ({backend}, {n} workers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
