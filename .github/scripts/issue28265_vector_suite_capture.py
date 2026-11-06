"""Run the standard vector suite while retaining each batch's native output."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


ROOT = Path.cwd()
OUT = Path(os.environ["VECTOR_EVIDENCE"])
OUT.mkdir(parents=True, exist_ok=True)
HEAD = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert HEAD == "2b4f45c3abdd4bd8ecc49b0ba6c69dd64e210bbc", HEAD
CACHE = Path("build/reldebug/CMakeCache.txt").read_text()
assert (
    "STANDARD_VECTOR_SIZE:STRING=2" in CACHE.splitlines()
    or "STANDARD_VECTOR_SIZE:UNINITIALIZED=2" in CACHE.splitlines()
)
assert not subprocess.check_output(["git", "diff", "--stat"], text=True)
sys.path.insert(0, str(ROOT))
from scripts.ci import run_tests


original_run_batch = run_tests.run_batch


def record_batch(config, batch):
    key = uuid.uuid4().hex
    metadata = {
        "batch": list(batch),
        "source_commit": HEAD,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config": vars(config),
        "completed": False,
    }
    path = OUT / (key + ".json")
    path.write_text(json.dumps(metadata, default=str, indent=2) + "\n")
    started = time.monotonic()
    result = original_run_batch(config, batch)
    for stream in ["stdout", "stderr"]:
        data = result[stream].encode("utf8")
        target = OUT / (key + "." + stream + ".log")
        target.write_bytes(data)
        metadata[stream] = {
            "path": target.name,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    metadata.update({k: v for k, v in result.items() if k not in ["stdout", "stderr"]})
    metadata.update(
        {
            "completed": True,
            "elapsed_s": time.monotonic() - started,
            "ended_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )
    path.write_text(json.dumps(metadata, default=str, indent=2) + "\n")
    return result


run_tests.run_batch = record_batch
arguments = [
    str(ROOT / "build/reldebug/test/unittest"),
    "--workers=3",
    "--batch-size=10",
    "--batch-timeout=600",
    "--retry=2",
    "--max-retries=4",
    "--track-runtime=100",
    *sys.argv[1:],
]
result = run_tests.main(arguments) or 0
(OUT / "suite-result.json").write_text(
    json.dumps({"commit": HEAD, "returncode": result, "arguments": arguments}, indent=2)
    + "\n"
)
raise SystemExit(result)
