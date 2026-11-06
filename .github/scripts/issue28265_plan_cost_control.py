import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path.cwd()
OUT = ROOT / "build/plan-cost-control"
OUT.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location(
    "plan_cost_runner", ROOT / "scripts/plan_cost_runner.py"
)
sys.path.insert(0, str(ROOT / "scripts"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
CLIS = {
    "base": ROOT / "build/base/release/duckdb",
    "fixed": ROOT / "build/current/release/duckdb",
}
BENCHMARK = ROOT / "benchmark/imdb_plan_cost"
QUERY = (BENCHMARK / "queries/05c.sql").read_text()


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def cli_json(cli, db, sql):
    result = subprocess.run(
        [str(cli), "-json", "--readonly", str(db), "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def plan_shape(op):
    return {
        "type": runner.get_physical_operator_type(op),
        "extra_info": op.get("extra_info", {}),
        "children": [plan_shape(child) for child in op.get("children", [])],
    }


identities = {
    name: {"path": str(cli), "sha256": digest(cli)} for name, cli in CLIS.items()
}
report = {
    "artifact_run": os.environ.get("ARTIFACT_RUN", "42184322469"),
    "source_commits": {
        "base": "c3dc01af116ce439464d582448b9c4d8dc412aec",
        "fixed_merge": "6c2370f259805caa9b8cbdbf0006a72472188f09",
        "fixed_head": "3c2fc2c925af936c72c0a97776515d0396a06488",
    },
    "identity_note": "Source revisions were verified from build checkout logs. The base artifact inherits the new version/hash metadata.",
    "binaries": identities,
    "query": QUERY,
    "completed": False,
    "measurements": [],
    "databases": {},
}


def save_report():
    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def save_failure(exc_type, error, traceback):
    report["failure"] = {"type": exc_type.__name__, "message": str(error)}
    save_report()
    sys.__excepthook__(exc_type, error, traceback)


sys.excepthook = save_failure
assert (
    report["artifact_run"] == "42184322469"
), "Source provenance is pinned to the observed failing run"
save_report()
for label in ["a", "b"]:
    db = OUT / f"base_init_{label}.duckdb"
    runner.init_db(str(CLIS["base"]), str(db), str(BENCHMARK), ["httpfs"])
    report["databases"][label] = {"path": str(db), "before_sha256": digest(db)}
    for phase, cli in CLIS.items():
        report["databases"][label][phase + "_identity"] = cli_json(
            cli,
            db,
            "SELECT version() AS version, current_setting('threads') AS threads",
        )
    save_report()

expected_rows = None
for repeat in range(3):
    order = ["base", "fixed"] if repeat % 2 == 0 else ["fixed", "base"]
    for label in ["a", "b"]:
        db = Path(report["databases"][label]["path"])
        for phase in order:
            profile = OUT / f"{label}_{phase}_{repeat}.profile.json"
            runner.PROFILE_FILENAME = str(profile)
            runner.PROFILE_OUTPUT = (
                "PRAGMA profile_output='" + str(profile).replace("'", "''") + "'"
            )
            start = time.monotonic()
            cost = runner.query_plan_cost(str(CLIS[phase]), str(db), QUERY)
            data = json.loads(profile.read_text())
            shape = plan_shape(runner.get_root_operator(data))
            shape_path = profile.with_suffix(".plan.json")
            shape_path.write_text(json.dumps(shape, indent=2) + "\n")
            row = {
                "database": label,
                "binary": phase,
                "repeat": repeat,
                "cost_total": cost.total,
                "cost_build": cost.build_side,
                "cost_probe": cost.probe_side,
                "elapsed_s": time.monotonic() - start,
                "profile": profile.name,
                "plan": shape_path.name,
                "plan_sha256": digest(shape_path),
            }
            report["measurements"].append(row)
            print(json.dumps(row), flush=True)
            save_report()

for label, entry in report["databases"].items():
    db = Path(entry["path"])
    for phase, cli in CLIS.items():
        rows = cli_json(cli, db, QUERY)
        entry[phase + "_rows"] = rows
        save_report()
        if expected_rows is None:
            expected_rows = rows
        assert rows == expected_rows, (label, phase, rows, expected_rows)
    entry["after_sha256"] = digest(db)
    save_report()
    assert entry["before_sha256"] == entry["after_sha256"], label
report["full_results_equal"] = True
report["read_only_database_hashes_unchanged"] = True
report["completed"] = True
save_report()
print(
    "CONTROL COMPLETE: full query results match; database hashes are unchanged.",
    flush=True,
)
