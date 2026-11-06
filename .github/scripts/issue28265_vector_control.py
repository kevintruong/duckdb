"""Collect native vector-size-two diagnostics; no CI test budgets are changed."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time


PHASE = sys.argv[1]
EXPECTED = {
    "fixed": "2b4f45c3abdd4bd8ecc49b0ba6c69dd64e210bbc",
    "base": "c3dc01af116ce439464d582448b9c4d8dc412aec",
}
ROOT = Path.cwd()
OUT = Path(os.environ["VECTOR_EVIDENCE"])
OUT.mkdir(parents=True, exist_ok=True)
REGRESSION = Path("test/optimizer/pushdown/join_filter_pushdown_struct.test")
REGRESSION.write_bytes(
    Path(os.environ["RUNNER_TEMP"], "struct-regression.test").read_bytes()
)
HEAD = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert HEAD == EXPECTED[PHASE], HEAD
CACHE = Path("build/reldebug/CMakeCache.txt").read_text()
assert (
    "STANDARD_VECTOR_SIZE:STRING=2" in CACHE
    or "STANDARD_VECTOR_SIZE:UNINITIALIZED=2" in CACHE
)
REPORT = {
    "collection_only": True,
    "phase": PHASE,
    "commit": HEAD,
    "platform": platform.platform(),
    "cpus": os.cpu_count(),
    "compiler": subprocess.check_output(["g++", "--version"], text=True),
    "cmake_vector_setting": [
        line for line in CACHE.splitlines() if "STANDARD_VECTOR_SIZE:" in line
    ],
    "binary_sha256": hashlib.sha256(
        Path("build/reldebug/test/unittest").read_bytes()
    ).hexdigest(),
    "regression_sha256": hashlib.sha256(REGRESSION.read_bytes()).hexdigest(),
    "tracked_diff": subprocess.check_output(["git", "diff", "--stat"], text=True),
    "tests": [],
}
assert not REPORT["tracked_diff"], REPORT["tracked_diff"]


def save():
    (OUT / f"{PHASE}.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def run_case(path, label, diagnostic=False):
    command = [
        "build/reldebug/test/unittest",
        str(path),
        "--use-colour",
        "no",
        "--durations",
        "yes",
    ]
    started = time.monotonic()
    log_path = OUT / f"{PHASE}-{label}.log"
    timed_out = False
    with log_path.open("w") as log:
        try:
            result = subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, timeout=600
            )
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = None
    output = log_path.read_text(errors="replace")
    item = {
        "label": label,
        "command": command,
        "diagnostic_only": diagnostic,
        "returncode": returncode,
        "outer_timeout": timed_out,
        "elapsed_s": time.monotonic() - started,
        "log": log_path.name,
        "all_tests_passed": returncode == 0 and "All tests passed" in output,
        "no_tests_matched": "No test cases matched" in output,
    }
    REPORT["tests"].append(item)
    save()
    print(json.dumps(item), flush=True)
    assert not item["no_tests_matched"], output
    return item


save()
try:
    for stem in ["cte_runtime_filter_modes", "cte_runtime_filter_sibling_dependencies"]:
        original = Path("test/sql/cte/materialized", stem + ".test")
        result = run_case(original, stem)
        if not result["all_tests_passed"]:
            source = original.read_text()
            old = "SET max_execution_time=10000;"
            assert source.count(old) == 1
            diagnostic = original.with_name(stem + "_budget_control.test")
            diagnostic.write_text(
                source.replace(str(original), str(diagnostic)).replace(
                    old, "SET max_execution_time=0;"
                )
            )
            try:
                run_case(diagnostic, stem + "_budget_control", diagnostic=True)
            finally:
                diagnostic.unlink()
    result = run_case(REGRESSION, "struct-regression")
    if PHASE == "fixed":
        assert result["all_tests_passed"], "Fixed engine must pass the regression"
    else:
        output = (OUT / result["log"]).read_text(errors="replace")
        assert result["returncode"] not in (None, 0), "Base must fail the regression"
        assert (
            str(REGRESSION) + ":18: FAILED" in output
        ), "Base must fail at the pruning assertion"
        assert re.search(
            r"Row Groups Scanned:\s*5\s*/\s*5", output
        ), "Base must scan every row group"
    REPORT["completed"] = True
finally:
    save()
