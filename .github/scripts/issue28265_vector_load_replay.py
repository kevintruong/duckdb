"""Replay the captured failing batch and overlapping work, without changing test budgets."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


ROOT = Path.cwd()
PHASE = sys.argv[1]
OUT = Path(os.environ["VECTOR_EVIDENCE"])
OUT.mkdir(parents=True, exist_ok=True)
EXPECTED = {
    "fixed": "2b4f45c3abdd4bd8ecc49b0ba6c69dd64e210bbc",
    "base": "c3dc01af116ce439464d582448b9c4d8dc412aec",
}
HEAD = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert HEAD == EXPECTED[PHASE]
assert not subprocess.check_output(["git", "diff", "--stat"], text=True)
CACHE = Path("build/reldebug/CMakeCache.txt").read_text().splitlines()
assert (
    "STANDARD_VECTOR_SIZE:STRING=2" in CACHE
    or "STANDARD_VECTOR_SIZE:UNINITIALIZED=2" in CACHE
)
INPUT = Path(os.environ["RUNNER_TEMP"], "vector-replay-input.json")
MANIFEST = json.loads(INPUT.read_text())
assert MANIFEST["artifact_run"] == 42270425866
sys.path.insert(0, str(ROOT))
from scripts.ci import run_tests


LOCK = threading.RLock()
REPORT = {
    "collection_only": True,
    "completed": False,
    "phase": PHASE,
    "commit": HEAD,
    "cpus": os.cpu_count(),
    "input_sha256": hashlib.sha256(INPUT.read_bytes()).hexdigest(),
    "tests_sha256": {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for entry in MANIFEST["batches"]
        for name in entry["batch"]
    },
    "target": MANIFEST["target"],
    "batches": [],
}
START = time.monotonic()


def save():
    with LOCK:
        (OUT / (PHASE + ".json")).write_text(json.dumps(REPORT, indent=2) + "\n")


def replay(entry):
    delay = START + entry["start_offset_s"] - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    config_values = dict(entry["config"])
    config_values["unittest_bin"] = str(ROOT / "build/reldebug/test/unittest")
    config_values["test_list"] = Path(config_values["test_list"])
    if config_values["coverage_profile_dir"] is not None:
        config_values["coverage_profile_dir"] = Path(
            config_values["coverage_profile_dir"]
        )
    config = run_tests.TestRunnerConfig(**config_values)
    launched = time.monotonic() - START
    result = run_tests.run_batch(config, entry["batch"])
    record = {
        "id": entry["id"],
        "batch": entry["batch"],
        "scheduled_start_s": entry["start_offset_s"],
        "actual_start_s": launched,
        "elapsed_s": time.monotonic() - START - launched,
    }
    for stream in ["stdout", "stderr"]:
        path = OUT / f"{PHASE}-{entry['id']}.{stream}.log"
        data = result[stream].encode("utf8")
        path.write_bytes(data)
        record[stream] = {"path": path.name, "sha256": hashlib.sha256(data).hexdigest()}
    record.update(
        {key: value for key, value in result.items() if key not in ["stdout", "stderr"]}
    )
    with LOCK:
        REPORT["batches"].append(record)
        save()
    print(
        json.dumps(
            {
                key: record[key]
                for key in [
                    "id",
                    "failed",
                    "returncode",
                    "elapsed_s",
                    "scheduled_start_s",
                    "actual_start_s",
                ]
            }
        ),
        flush=True,
    )


save()
try:
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(replay, entry) for entry in MANIFEST["batches"]]
        for future in futures:
            future.result()
    assert len(REPORT["batches"]) == len(MANIFEST["batches"]) == 9
    REPORT["completed"] = True
finally:
    save()
