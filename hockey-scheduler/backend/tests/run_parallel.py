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
"""

import argparse
import concurrent.futures
import glob
import os
import subprocess
import sys
import time


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
        for shard, code, out, err in ex.map(_run_shard, jobs):
            tail = (err or out).strip().splitlines()[-1:] or [""]
            status = "ok" if code == 0 else "FAIL"
            print(f"  [{status}] {' '.join(shard)[:70]}…  {tail[-1]}")
            if code != 0:
                failed.append((shard, out, err))

    elapsed = time.time() - t0
    if failed:
        print("\n===== FAILURES =====")
        for shard, out, err in failed:
            print(f"\n--- {' '.join(shard)} ---\n{err[-4000:]}\n{out[-2000:]}")
        print(f"\nFAILED ({len(failed)} shard(s)) in {elapsed:.0f}s")
        return 1
    print(f"\nOK — all shards passed in {elapsed:.0f}s ({backend}, {n} workers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
