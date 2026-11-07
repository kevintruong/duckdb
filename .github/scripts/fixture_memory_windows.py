"""Measure the historically failing CTAS, using one preserved native x86 CLI."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time

OUT = Path('fixture-memory')
OUT.mkdir(exist_ok=True)
BIN = Path('duckdb.exe').resolve()
raw = BIN.read_bytes()
offset = struct.unpack_from('<I', raw, 0x3c)[0]
assert struct.unpack_from('<H', raw, offset + 4)[0] == 0x14c
assert struct.unpack_from('<H', raw, offset + 24)[0] == 0x10b
large_address_aware = bool(struct.unpack_from('<H', raw, offset + 22)[0] & 0x20)
assert not large_address_aware
digest = hashlib.sha256(raw).hexdigest()
os.environ['PATH'] = str(Path('src').resolve()) + os.pathsep + os.environ['PATH']

class ProcessMemory(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
        'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
        'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]

psapi = ctypes.WinDLL('psapi', use_last_error=True)
get_memory = psapi.GetProcessMemoryInfo
get_memory.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessMemory), wintypes.DWORD)
get_memory.restype = wintypes.BOOL
report = []
test = 'test/sql/unnest/unnest_string_constant_reference.test'
for phase, ref in [('control', None), ('before', 'd4eea3f208d42ca4ee522aca23f5ea76f902b0ca'),
                   ('after', 'a662f0340baa5955b2331aeb7ff7bae120657e79')]:
    sql = 'SELECT 1;'
    if ref:
        source = subprocess.check_output(['git', 'show', ref + ':' + test]).decode('utf8')
        sql = '\n'.join(re.findall(r'^CREATE TABLE (?:strs|unnested) AS .*;$', source, re.M))
        assert sql.count('CREATE TABLE') == 2
        sql += '\nSELECT count(*),sum(strlen(s)),sum(strlen(st.s)) FROM unnested;'
    (OUT / (phase + '.sql')).write_text(sql, encoding='utf8')
    peaks = {'peak_working_set_bytes': 0, 'peak_pagefile_bytes': 0, 'observed_peak_private_bytes': 0}
    samples = 0
    started = time.monotonic()
    with (OUT / (phase + '.stdout.log')).open('wb') as stdout, (OUT / (phase + '.stderr.log')).open('wb') as stderr:
        process = subprocess.Popen([str(BIN), '-init', 'NUL', '-csv'], stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
        process.stdin.write(sql.encode('utf8'))
        process.stdin.close()
        try:
            while True:
                counters = ProcessMemory()
                counters.cb = ctypes.sizeof(counters)
                if get_memory(int(process._handle), ctypes.byref(counters), counters.cb):
                    samples += 1
                    peaks['peak_working_set_bytes'] = max(peaks['peak_working_set_bytes'], counters.PeakWorkingSetSize)
                    peaks['peak_pagefile_bytes'] = max(peaks['peak_pagefile_bytes'], counters.PeakPagefileUsage)
                    peaks['observed_peak_private_bytes'] = max(peaks['observed_peak_private_bytes'], counters.PrivateUsage)
                if process.poll() is not None:
                    break
                if time.monotonic() - started > 60:
                    raise TimeoutError(phase)
                time.sleep(0.005)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    row = {'phase': phase, 'fixture_commit': ref, 'binary_sha256': digest,
           'large_address_aware': large_address_aware, 'returncode': process.returncode,
           'elapsed_s': time.monotonic() - started, 'samples': samples, **peaks}
    report.append(row)
    (OUT / 'memory.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(row), flush=True)
    assert samples > 0 and peaks['peak_working_set_bytes'] > 0
    output = (OUT / (phase + '.stdout.log')).read_bytes() + (OUT / (phase + '.stderr.log')).read_bytes()
    if phase == 'before':
        assert process.returncode != 0 and b'Allocation failure' in output, output
    elif phase == 'after':
        assert process.returncode == 0 and b'25000,54665000,54665000' in output, output
    else:
        assert process.returncode == 0, output
