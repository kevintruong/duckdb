"""Verify restored fixture inputs and the explicit architecture requirement."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

PLATFORM = sys.argv[1]
OUT = Path('review-platform-evidence')
OUT.mkdir(exist_ok=True)
HEAD = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
assert HEAD == os.environ['FIXTURE_COMMIT']
ORIGIN = 'a662f0340baa5955b2331aeb7ff7bae120657e79'
BASE = 'd4eea3f208d42ca4ee522aca23f5ea76f902b0ca'
assert not subprocess.check_output(['git','diff','--name-only',ORIGIN,HEAD,'--','src','extension','third_party','CMakeLists.txt','Makefile'], text=True)
HEAVY = 'test/sql/unnest/unnest_string_constant_reference.test'
SCAN = 'test/sql/unnest/unnest_string_constant_reference_scan.test'
FILES = [HEAVY, SCAN, 'test/sql/cte/materialized/cte_runtime_filter_modes.test',
         'test/sql/cte/materialized/cte_runtime_filter_sibling_dependencies.test']
original = subprocess.check_output(['git','show',BASE + ':' + HEAVY]).decode('utf8')
heavy = Path(HEAVY).read_text(encoding='utf8')
scan = Path(SCAN).read_text(encoding='utf8')

def commands(text):
    return '\n'.join(line for line in text.splitlines() if line.strip() and not line.startswith('#') and not line.startswith('require '))

assert commands(original) == commands(heavy + scan)
assert "repeat('ab', 50000)" in heavy and "repeat('é', 30000)" in heavy
assert 'require 64bit' in heavy and 'require 64bit' not in scan
assert 'require block_size 262144' in heavy and 'require block_size 262144' in scan
if PLATFORM == 'windows32':
    BIN = Path('test/unittest.exe').resolve()
    raw = BIN.read_bytes()
    offset = struct.unpack_from('<I', raw, 0x3c)[0]
    assert struct.unpack_from('<H', raw, offset + 4)[0] == 0x14c
    assert struct.unpack_from('<H', raw, offset + 24)[0] == 0x10b
    os.environ['PATH'] = str(Path('src').resolve()) + os.pathsep + os.environ['PATH']
else:
    BIN = Path('build/reldebug/test/unittest').resolve()
    assert BIN.read_bytes()[:5] == b'\x7fELF\x02'
    cache = Path('build/reldebug/CMakeCache.txt').read_text().splitlines()
    assert 'STANDARD_VECTOR_SIZE:STRING=2' in cache or 'STANDARD_VECTOR_SIZE:UNINITIALIZED=2' in cache
    os.environ['LD_LIBRARY_PATH'] = str(Path('build/reldebug/src').resolve()) + os.pathsep + os.environ.get('LD_LIBRARY_PATH', '')

def run(name, args):
    started = time.monotonic()
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    for stream in ('stdout', 'stderr'):
        (OUT / (name + '.' + stream + '.log')).write_bytes(getattr(result, stream))
    print(json.dumps({'phase':name,'returncode':result.returncode,'elapsed_s':time.monotonic()-started}), flush=True)
    print(result.stdout.decode('utf8',errors='replace')[-2000:], flush=True)
    assert result.returncode == 0
    text = (result.stdout + result.stderr).decode('utf8',errors='replace')
    assert 'No test cases matched' not in text and 'No tests ran' not in text
    return text

listing = OUT / 'sql.list'
listing.write_text('\n'.join(FILES)+'\n')
sql = run('sql', [str(BIN), '-f', str(listing.resolve()), '--emit-test-events', '--use-colour', 'no'])
assert re.search(r'in 4 test cases', sql)
events = [json.loads(line.split('[TEST_EVENT] ',1)[1]) for line in sql.splitlines() if '[TEST_EVENT] ' in line]
ends = {}
for path in FILES:
    matches = [e for e in events if e.get('event') == 'end' and e.get('name') == path]
    assert len(matches) == 1
    event = matches[0]
    if PLATFORM == 'windows32' and path == HEAVY:
        assert event['status'] == 'skip-requirement' and event['data'] == 'require 64bit'
    else:
        assert event['status'] == 'ok' and event['passes'] > 0 and event['fails'] == 0 and event['skip-mode'] == 0
    ends[path] = event
api = run('api', [str(BIN), 'A constant reference*', '--use-colour', 'no'])
assert re.search(r'in 3 test cases', api)
report = {'fixture_commit':HEAD,'binary_engine_commit':ORIGIN,'binary_origin_run':42313956408,
          'platform':PLATFORM,'original_sql_and_expected_results_preserved':True,
          'ascii_repeats':50000,'unicode_repeats':30000,'sql_end_events':ends,
          'api_case_count':3,'sql_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in FILES},
          'binary_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest()}
(OUT / 'review-result.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'platform':PLATFORM,'fixture_commit':HEAD,'original_inputs_preserved':True,
                  'heavy_skipped':PLATFORM=='windows32','scan_executed':True,'api_cases':3}), flush=True)
