"""One-off read-only PostgreSQL export for the Rust product importer.

Use the legacy migration image; this is not a runtime dependency. The source
transaction is repeatable-read/read-only and no existing destination is opened.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3

import psycopg
from psycopg import sql


def export(source, destination, schema, extensions=None):
    target = Path(destination).resolve()
    if target.exists():
        raise ValueError('Destination exists; refusing to overwrite')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name('.' + target.name + '.exporting')
    if staging.exists():
        raise ValueError('An unfinished export exists; inspect it before retrying')
    os.close(os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    counts = {}
    try:
        with psycopg.connect(source) as original:
            original.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            active = original.execute("SELECT count(*) FROM runs WHERE status NOT IN ('completed','cancelled','needs_attention')").fetchone()[0]
            if active:
                raise ValueError('Active legacy runs must be stopped before export')
            tables = original.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
            with sqlite3.connect(staging) as copied:
                copied.executescript(Path(schema).read_text())
                copied.executescript(Path(extensions or Path(schema).with_name('extensions.sql')).read_text())
                expected_tables = {row[0] for row in copied.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
                for (table,) in tables:
                    columns = original.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (table,)).fetchall()
                    names = [name for name, _ in columns]
                    quote = lambda value: '"' + value.replace('"', '""') + '"'
                    sqlite_types = {'integer': 'INTEGER', 'bigint': 'INTEGER', 'boolean': 'INTEGER', 'double precision': 'REAL', 'real': 'REAL', 'bytea': 'BLOB'}
                    primary = original.execute("SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY array_position(i.indkey,a.attnum)", (table,)).fetchall()
                    definitions = [quote(name) + ' ' + sqlite_types.get(kind, 'TEXT') for name, kind in columns]
                    if primary:
                        definitions.append('PRIMARY KEY (' + ','.join(quote(name) for (name,) in primary) + ')')
                    if table not in expected_tables:
                        copied.execute('CREATE TABLE ' + quote(table) + ' (' + ', '.join(definitions) + ')')
                    counts[table] = 0
                    if table == 'sessions':
                        continue
                    with original.cursor(name='forge_export_' + table) as cursor:
                        cursor.execute(sql.SQL('SELECT {} FROM {}').format(sql.SQL(',').join(map(sql.Identifier, names)), sql.Identifier(table)))
                        while rows := cursor.fetchmany(500):
                            values = [[json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else int(value) if isinstance(value, bool) else value.isoformat() if hasattr(value, 'isoformat') else value for value in row] for row in rows]
                            copied.executemany('INSERT INTO ' + quote(table) + ' (' + ','.join(map(quote, names)) + ') VALUES (' + ','.join('?' for _ in names) + ')', values)
                            counts[table] += len(values)
                if copied.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('SQLite export failed integrity check')
        os.chmod(staging, 0o600)
        os.link(staging, target)
        staging.unlink()
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--schema', default=str(Path(__file__).resolve().parents[1] / 'rust/crates/forge-server/src/product_store/schema.sql'))
    parser.add_argument('--extensions', help='Additional product schema; defaults to extensions.sql next to --schema')
    args = parser.parse_args()
    source_url = os.environ.get('FORGE_POSTGRES_EXPORT_URL', '').replace('postgresql+psycopg://', 'postgresql://')
    if not source_url:
        parser.error('Set FORGE_POSTGRES_EXPORT_URL for the read-only source')
    try:
        print(json.dumps({'ok': True, 'counts': export(source_url, args.output, args.schema, args.extensions)}))
    except Exception:
        raise SystemExit('PostgreSQL export failed; source remains unchanged. Inspect source availability and destination before retrying.')
