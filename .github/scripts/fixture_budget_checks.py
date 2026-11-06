"""Fork-only diagnostics; the upstream change contains only three SQL tests."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import uuid

ROOT = Path.cwd()
OUT = ROOT / 'fixture-evidence'
OUT.mkdir(exist_ok=True)
HEAD = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
assert HEAD == 'a662f0340baa5955b2331aeb7ff7bae120657e79', HEAD
BASE = 'd4eea3f208d42ca4ee522aca23f5ea76f902b0ca'
MODE = sys.argv[1]
FILES = [
    'test/sql/unnest/unnest_string_constant_reference.test',
    'test/sql/cte/materialized/cte_runtime_filter_modes.test',
    'test/sql/cte/materialized/cte_runtime_filter_sibling_dependencies.test',
]
BIN = ROOT / ('test/unittest.exe' if MODE == 'windows' else 'build/reldebug/test/unittest')


def run(name, arguments, timeout=600):
    started = time.monotonic()
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    for stream in ('stdout', 'stderr'):
        (OUT / (name + '.' + stream + '.log')).write_bytes(getattr(result, stream))
    report = {'commit': HEAD, 'arguments': list(map(str, arguments)), 'returncode': result.returncode,
              'elapsed_s': time.monotonic() - started,
              'sql_sha256': {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in FILES}}
    (OUT / (name + '.json')).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)
    print(result.stdout.decode(errors='replace')[-3000:], flush=True)
    print(result.stderr.decode(errors='replace')[-3000:], flush=True)
    return result


if MODE == 'windows':
    pe = BIN.read_bytes()
    offset = struct.unpack_from('<I', pe, 0x3c)[0]
    machine = struct.unpack_from('<H', pe, offset + 4)[0]
    magic = struct.unpack_from('<H', pe, offset + 24)[0]
    characteristics = struct.unpack_from('<H', pe, offset + 22)[0]
    assert machine == 0x14c and magic == 0x10b, (machine, magic)
    (OUT / 'architecture.json').write_text(json.dumps({'machine': machine, 'magic': magic,
        'large_address_aware': bool(characteristics & 0x20)}, indent=2) + '\n')
    path = Path(FILES[0])
    fixed = path.read_bytes()
    try:
        path.write_bytes(subprocess.check_output(['git', 'show', BASE + ':' + FILES[0]]))
        old = run('old-unnest', [str(BIN), FILES[0]])
        assert old.returncode != 0 and b'Allocation failure' in old.stdout + old.stderr, 'Old fixture did not reproduce the historical OOM'
    finally:
        path.write_bytes(fixed)

if MODE in ('windows', 'vector'):
    if MODE == 'vector':
        cache = Path('build/reldebug/CMakeCache.txt').read_text()
        assert 'STANDARD_VECTOR_SIZE:STRING=2' in cache or 'STANDARD_VECTOR_SIZE:UNINITIALIZED=2' in cache
    focused = run('fixed-focused', [str(BIN), ','.join(FILES) + ',A constant reference*', '--emit-test-events'])
    assert focused.returncode == 0
    events = focused.stdout + focused.stderr
    assert b'No tests ran' not in events and b'No test cases matched' not in events
    assert b'"event":"skip"' not in events and b'"event": "skip"' not in events
    records = [json.loads(line.split('[TEST_EVENT] ', 1)[1]) for line in events.decode(errors='replace').splitlines()
               if '[TEST_EVENT] ' in line]
    for path in FILES:
        ends = [r for r in records if r.get('event') == 'end' and r.get('name') == path]
        assert len(ends) == 1 and ends[0]['status'] == 'ok' and ends[0]['passes'] > 0 and ends[0]['skip-mode'] == 0, ends
    assert b'6 test cases' in events, 'The three SQL and three API tests must all execute'
    subprocess.run(['git', 'diff', '--exit-code'], check=True)

elif MODE == 'suite':
    sys.path.insert(0, str(ROOT))
    from scripts.ci import run_tests
    original = run_tests.run_batch

    def capture(config, batch):
        key = uuid.uuid4().hex
        started = time.monotonic()
        result = original(config, batch)
        record = {'commit': HEAD, 'batch': list(batch), 'elapsed_s': time.monotonic() - started}
        for stream in ('stdout', 'stderr'):
            (OUT / (key + '.' + stream + '.log')).write_text(result[stream])
        record.update({k: v for k, v in result.items() if k not in ('stdout', 'stderr')})
        (OUT / (key + '.json')).write_text(json.dumps(record, indent=2) + '\n')
        return result

    run_tests.run_batch = capture
    result = run_tests.main([str(BIN), '--workers=3', '--batch-size=10', '--batch-timeout=600',
                             '--retry=2', '--max-retries=4', '--track-runtime=100']) or 0
    (OUT / 'suite-result.json').write_text(json.dumps({'commit': HEAD, 'returncode': result}) + '\n')
    raise SystemExit(result)
