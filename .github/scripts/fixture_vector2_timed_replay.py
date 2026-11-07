"""Replay the unchanged timed-out batch without sharing one deadline across ten files."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path.cwd()
OUT = ROOT / 'fixture-replay'
OUT.mkdir(exist_ok=True)
HEAD = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
assert HEAD == 'a662f0340baa5955b2331aeb7ff7bae120657e79'
assert not subprocess.check_output(['git', 'diff', '--stat'], text=True)
CACHE = Path('build/reldebug/CMakeCache.txt').read_text().splitlines()
assert 'STANDARD_VECTOR_SIZE:STRING=2' in CACHE or 'STANDARD_VECTOR_SIZE:UNINITIALIZED=2' in CACHE
INPUT = Path(os.environ['RUNNER_TEMP']) / 'fixture_vector2_timed_replay.list'
EXPECTED = INPUT.read_text().splitlines()
assert len(EXPECTED) == 13 and len(set(EXPECTED)) == 13
CHANGED = [p for p in EXPECTED if '/cte/' in p or '/unnest/' in p]
assert len(CHANGED) == 3
sys.path.insert(0, str(ROOT))
from scripts.ci import run_tests

original = run_tests.run_batch
records = []

def capture(config, batch):
    assert len(batch) == 1 and config.batch_timeout_seconds == 600
    key = uuid.uuid4().hex
    started = time.monotonic()
    result = original(config, batch)
    record = {'commit': HEAD, 'batch': list(batch), 'elapsed_s': time.monotonic() - started,
              'sql_sha256': {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in batch}}
    for stream in ('stdout', 'stderr'):
        (OUT / (key + '.' + stream + '.log')).write_text(result[stream])
    record.update({k: v for k, v in result.items() if k not in ('stdout', 'stderr')})
    (OUT / (key + '.json')).write_text(json.dumps(record, indent=2) + '\n')
    records.append((record, result))
    return result

run_tests.run_batch = capture
arguments = [str(ROOT / 'build/reldebug/test/unittest'), '--test-list', str(INPUT),
             '--workers=3', '--batch-size=1', '--batch-timeout=600', '--retry=2',
             '--max-retries=4', '--track-runtime=100']
result = run_tests.main(arguments) or 0
report = {'commit': HEAD, 'binary_origin_run': 42313956408, 'returncode': result,
          'exact_vector_size': 2, 'workers': 3, 'batch_size': 1, 'batch_timeout_seconds': 600,
          'expected_files': EXPECTED, 'events': {}}
for record, streams in records:
    if record.get('failed'):
        continue
    path = record['batch'][0]
    events = []
    for text in (streams['stdout'], streams['stderr']):
        for line in text.splitlines():
            if '[TEST_EVENT] ' in line:
                events.append(json.loads(line.split('[TEST_EVENT] ', 1)[1]))
    report['events'][path] = [e for e in events if e.get('event') == 'end' or 'skip' in e.get('event', '')]
(OUT / 'replay-result.json').write_text(json.dumps(report, indent=2) + '\n')
if result == 0:
    assert set(report['events']) == set(EXPECTED)
    for path, events in report['events'].items():
        ends = [e for e in events if e.get('event') == 'end' and e.get('passes', 0) > 0 and e.get('fails') == 0]
        skips = [e for e in events if 'skip' in e.get('event', '')]
        if path.endswith('hash_join_residual_predicates.test'):
            assert skips and all('tpch' in json.dumps(e).lower() for e in skips), (path, events)
            continue
        assert ends, (path, events)
        if path in CHANGED:
            assert not skips and all(e.get('skip-mode') == 0 for e in ends), (path, events)
        else:
            assert not skips, (path, events)
print(json.dumps({'returncode': result, 'files': len(report['events']), 'original_batch_timeout_retained_in_history': True}))
raise SystemExit(result)
