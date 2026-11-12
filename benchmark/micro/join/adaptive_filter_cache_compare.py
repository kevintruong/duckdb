"""Compare shared filter caching against the original PR using two DuckDB CLIs."""

import argparse
import hashlib
import json
import pathlib
import statistics
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--before', type=pathlib.Path, required=True, help='CLI built from the original PR head')
parser.add_argument('--after', type=pathlib.Path, required=True, help='CLI built with the shared fallback')
parser.add_argument('--workdir', type=pathlib.Path, required=True, help='Empty directory for fixtures and results')
parser.add_argument('--rounds', type=int, default=6)
args = parser.parse_args()
if args.rounds < 1:
    parser.error('--rounds must be positive')
ROOT = args.workdir.resolve()
if ROOT.exists() and any(ROOT.iterdir()):
    parser.error('--workdir must be empty')
ROOT.mkdir(parents=True, exist_ok=True)
BEFORE = args.before.resolve(strict=True)
AFTER = args.after.resolve(strict=True)
DB = ROOT / 'benchmark.duckdb'


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run(binary, sql, database=None):
    command = [str(binary), '-json']
    if database is not None:
        command.append(str(database))
    command.extend(['-c', sql])
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    decoder = json.JSONDecoder()
    text = result.stdout.strip()
    values = []
    while text:
        value, offset = decoder.raw_decode(text)
        values.append(value)
        text = text[offset:].lstrip()
    return values


def scan_rows(plan, name):
    found = []

    def visit(value):
        if isinstance(value, dict):
            if value.get('type') == 'TABLE_SCAN':
                extra = value.get('extra_info', {})
                if name in extra.get('Table', '') or name in extra.get('Filename(s)', ''):
                    found.append(value['intermediate_rows'])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(plan)
    assert len(found) == 1, found
    return found[0]


if not DB.exists():
    statements = [
        "SET threads=1",
        "SET force_compression='rle'",
        "CREATE TABLE numeric_build AS SELECT 1::BIGINT w,1::BIGINT d,k,k>=2700 keep FROM range(1,3001) t(k)",
    ]
    cases = {
        'selective': 'i%122880<12288',
        'broad': 'i%122880>=12288',
        'skewed': 'i%2048<>0',
    }
    for name, condition in cases.items():
        statements.append(
            f"CREATE TABLE rle_{name} AS SELECT 1::BIGINT w,1::BIGINT d,CASE WHEN {condition} THEN 3000 ELSE 1 END::BIGINT k,1::BIGINT n FROM range(6*122880) t(i)"
        )
    statements += [
        'CHECKPOINT',
        "SET force_compression='dict_fsst'",
        "CREATE TABLE string_build AS SELECT w,d,lpad(k::VARCHAR,4,'0')||repeat('x',40) k,keep FROM numeric_build",
    ]
    for name in cases:
        statements.append(
            f"CREATE TABLE dict_{name} AS SELECT w,d,lpad(k::VARCHAR,4,'0')||repeat('x',40) k,n FROM rle_{name}"
        )
    statements.append('CHECKPOINT')
    for name in cases:
        for kind in ('rle', 'dict'):
            path = ROOT / f'{kind}_{name}.parquet'
            statements.append(f"COPY {kind}_{name} TO {quote(path)} (FORMAT PARQUET,ROW_GROUP_SIZE 122880)")
    run(BEFORE, ';'.join(statements), DB)

prefix = "SET threads=1;SET disabled_optimizers='statistics_propagation,join_order,build_side_probe_side';SET dynamic_or_filter_threshold=0;SET parquet_use_page_index=false;"
records = []
for kind in ('rle', 'dict'):
    build = 'numeric_build' if kind == 'rle' else 'string_build'
    for name in ('selective', 'broad', 'skewed'):
        table = f'{kind}_{name}'
        encoding = 'RLE' if kind == 'rle' else 'DICT_FSST'
        storage_check = run(
            BEFORE,
            f"SELECT count(*)>0 AND bool_and(compression={quote(encoding)}) AS valid "
            f"FROM pragma_storage_info({quote(table)}) WHERE column_name='k' AND segment_type<>'VALIDITY'",
            DB,
        )[0][0]
        assert storage_check['valid'], (table, encoding, storage_check)
        for storage in ('native', 'parquet'):
            source = table if storage == 'native' else f"read_parquet({quote(ROOT / (table + '.parquet'))})"
            query = f'SELECT count(*) AS matches,sum(p.n) AS total FROM {source} p JOIN {build} b ON p.w=b.w AND p.d=b.d AND p.k=b.k WHERE b.keep'
            timings = {'before': [], 'after': []}
            rows = {'before': [], 'after': []}
            expected = {'selective': 6 * 12288, 'broad': 6 * (122880 - 12288), 'skewed': 6 * (122880 - 60)}[name]
            for label, binary in [('before', BEFORE), ('after', AFTER)]:
                actual = run(binary, prefix + query, DB)[0][0]
                assert int(actual['matches']) == expected and int(actual['total']) == expected, (label, table, actual)
            for repetition in range(args.rounds):
                order = [('before', BEFORE), ('after', AFTER)]
                if repetition % 2:
                    order.reverse()
                for label, binary in order:
                    plans = run(binary, prefix + ';'.join(['EXPLAIN (ANALYZE,FORMAT JSON) ' + query] * 4), DB)
                    assert len(plans) == 4, len(plans)
                    for plan in plans[1:]:
                        timings[label].append(plan['query']['total_time'])
                        rows[label].append(scan_rows(plan, table))
            before = statistics.median(timings['before'])
            after = statistics.median(timings['after'])
            record = {
                'kind': kind,
                'layout': name,
                'storage': storage,
                'before_ms': round(1000 * before, 3),
                'after_ms': round(1000 * after, 3),
                'change_pct': round(100 * (after / before - 1), 2),
                'before_scan_rows': sorted(set(rows['before'])),
                'after_scan_rows': sorted(set(rows['after'])),
                'samples': timings,
            }
            records.append(record)
            print(json.dumps({k: v for k, v in record.items() if k != 'samples'}), flush=True)
metadata = {
    'rows': 6 * 122880,
    'rounds': args.rounds,
    'warmup_queries_per_process': 1,
    'measured_queries_per_process': 3,
    'threads': 1,
    'before_sha256': hashlib.sha256(BEFORE.read_bytes()).hexdigest(),
    'after_sha256': hashlib.sha256(AFTER.read_bytes()).hexdigest(),
    'comparison': 'Original PR #28515 versus shared fallback; not the version comparison in issue #28269',
    'scope': 'Small warm fixture comparison of the original PR and shared fallback; no acceptance threshold or general speedup claim.',
}
(ROOT / 'benchmark-results.json').write_text(json.dumps({'metadata': metadata, 'cases': records}, indent=2) + '\n')
