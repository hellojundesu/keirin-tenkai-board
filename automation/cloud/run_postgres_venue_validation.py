"""Run the venue-generation SQL suite against an isolated PostgreSQL cluster."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str], *, env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, check=check, text=True, capture_output=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-bin", required=True, type=Path)
    parser.add_argument("--keep-cluster", action="store_true")
    args = parser.parse_args()
    pg_bin = args.pg_bin.resolve()
    executables = {name: pg_bin / f"{name}.exe" for name in ("initdb", "pg_ctl", "createdb", "psql")}
    missing = [str(path) for path in executables.values() if not path.exists()]
    if missing:
        raise SystemExit("missing PostgreSQL executables: " + ", ".join(missing))

    validation_root = ROOT / "work"
    validation_root.mkdir(parents=True, exist_ok=True)
    cluster = Path(tempfile.mkdtemp(prefix="venue-pg-", dir=validation_root))
    data = cluster / "data"
    log = cluster / "postgres.log"
    port = free_port()
    env = os.environ.copy()
    env.update(PGHOST="127.0.0.1", PGPORT=str(port), PGUSER="postgres", PGDATABASE="venue_validation")
    report = {"postgres_bin": str(pg_bin), "port": port, "cluster": str(cluster), "checks": {}}
    started = False
    try:
        run([str(executables["initdb"]), "-D", str(data), "-U", "postgres", "--auth=trust", "--encoding=UTF8", "--no-locale"], env=env)
        run([str(executables["pg_ctl"]), "-D", str(data), "-l", str(log), "-o", f"-p {port} -h 127.0.0.1", "start", "-w"], env=env)
        started = True
        createdb_env = {**env, "PGDATABASE": "postgres"}
        run([str(executables["createdb"]), "venue_validation"], env=createdb_env)
        run([str(executables["psql"]), "-v", "ON_ERROR_STOP=1", "-f", str(ROOT / "database" / "venue_generation_publication.sql")], env=env)
        functional = run([str(executables["psql"]), "-v", "ON_ERROR_STOP=1", "-f", str(ROOT / "tests" / "postgres" / "venue_generation_publication_test.sql")], env=env)
        report["checks"]["functional_sql"] = "pass" if "PASS" in functional.stdout else "unknown"

        # Two candidates for the same not-yet-active venue are staged, then
        # published simultaneously with expected revision zero. Exactly one
        # transaction may win; the other must fail with revision_conflict.
        setup = ROOT / "tests" / "postgres" / "venue_generation_concurrency_setup.sql"
        run([str(executables["psql"]), "-v", "ON_ERROR_STOP=1", "-f", str(setup)], env=env)
        calls = []
        for generation in ("d", "e"):
            sql = (
                "select public.cloud_publish_venue_generation("
                "'racecard','2026-11-19','03',repeat('" + generation + "',64),0,'concurrency-test');"
            )
            calls.append(subprocess.Popen([str(executables["psql"]), "-v", "ON_ERROR_STOP=1", "-c", sql], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        outcomes = [process.communicate() + (process.returncode,) for process in calls]
        success = sum(return_code == 0 for _, _, return_code in outcomes)
        conflicts = sum("revision_conflict" in stderr for _, stderr, _ in outcomes)
        if success != 1 or conflicts != 1:
            raise RuntimeError(f"concurrency check failed: {outcomes!r}")
        verify = run([
            str(executables["psql"]), "-At", "-c",
            "select count(*) || ':' || min(revision) || ':' || max(revision) from public.cloud_active_venue_generations where race_date='2026-11-19' and venue_code='03'"
        ], env=env)
        if verify.stdout.strip() != "1:1:1":
            raise RuntimeError("concurrency winner pointer is invalid: " + verify.stdout)
        report["checks"]["concurrent_publish_one_winner"] = "pass"
        report["status"] = "pass"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        if started:
            subprocess.run([str(executables["pg_ctl"]), "-D", str(data), "stop", "-m", "fast", "-w"], env=env, text=True, capture_output=True)
        if not args.keep_cluster:
            shutil.rmtree(cluster, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
