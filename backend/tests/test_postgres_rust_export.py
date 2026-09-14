"""Validate the PostgreSQL export boundary without requiring a database daemon."""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

pytest.importorskip('psycopg', reason='PostgreSQL export uses requirements-temporal.txt')


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('postgres_export', ROOT / 'scripts/export-postgres-to-sqlite.py')
EXPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORTER)


class Rows:
    def __init__(self, values):
        self.values = values

    def fetchall(self):
        return self.values

    def fetchone(self):
        return self.values[0]


class Source:
    columns = {
        'users': [('id', 'character varying'), ('username', 'character varying'),
                  ('password_hash', 'text'), ('role', 'character varying'), ('enabled', 'boolean')],
        'design_audit': [('id', 'character varying'), ('asset_id', 'character varying'),
                         ('version', 'integer'), ('digest', 'character varying'),
                         ('action', 'character varying'), ('actor', 'character varying'),
                         ('comment', 'text'), ('created_at', 'character varying')],
    }
    data = {
        'users': [('admin-id', 'admin', 'fixture-password-hash', 'admin', True)],
        'design_audit': [('audit-id', 'asset-id', 2, 'digest', 'publish', 'admin-id',
                          'Preserved history', '2026-09-12T00:00:00Z')],
    }

    def __init__(self, active=0):
        self.active = active
        self.read_only = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=()):
        if query.startswith('BEGIN TRANSACTION'):
            assert 'REPEATABLE READ READ ONLY' in query
            self.read_only = True
            return Rows([])
        assert self.read_only
        if query.startswith('SELECT count(*) FROM runs'):
            return Rows([(self.active,)])
        if 'information_schema.tables' in query:
            return Rows([(name,) for name in self.columns])
        if 'information_schema.columns' in query:
            return Rows(self.columns[params[0]])
        if 'FROM pg_index' in query:
            return Rows([('id',)])
        raise AssertionError(query)

    def cursor(self, name):
        source = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, query):
                table = name.removeprefix('forge_export_')
                assert f'FROM "{table}"' in query.as_string()
                self.rows = source.data[table]

            def fetchmany(self, _):
                rows, self.rows = self.rows, []
                return rows

        return Cursor()


def test_export_preserves_extension_schema_and_is_accepted_by_rust(tmp_path, monkeypatch):
    source = Source()
    monkeypatch.setattr(EXPORTER.psycopg, 'connect', lambda _: source)
    target = tmp_path / 'legacy.sqlite3'
    schema = ROOT / 'rust/crates/forge-server/src/product_store/schema.sql'
    assert EXPORTER.export('fixture', target, schema) == {'users': 1, 'design_audit': 1}
    assert source.read_only
    assert target.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(target) as db:
        fields = {row[1]: (row[2], row[3]) for row in db.execute('PRAGMA table_info(design_audit)')}
        assert fields['asset_id'] == ('VARCHAR(32)', 1)
        assert db.execute('SELECT comment FROM design_audit').fetchone()[0] == 'Preserved history'
    before = target.read_bytes()
    binary = ROOT / 'rust/target/debug/forge-server'
    assert binary.is_file(), 'Build the Rust HTTP host before migration contract tests'
    env = {key: value for key, value in os.environ.items() if key != 'ENCRYPTION_KEY'}
    result = subprocess.run([str(binary), '--product-db', str(tmp_path / 'product.sqlite3'),
                             '--import-db', str(target), '--migrate-only'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['source_preserved']
    assert target.read_bytes() == before
    with pytest.raises(ValueError, match='refusing to overwrite'):
        EXPORTER.export('fixture', target, schema)


def test_export_rejects_active_runs_without_publishing(tmp_path, monkeypatch):
    monkeypatch.setattr(EXPORTER.psycopg, 'connect', lambda _: Source(active=1))
    target = tmp_path / 'legacy.sqlite3'
    with pytest.raises(ValueError, match='Active legacy runs'):
        EXPORTER.export('fixture', target, ROOT / 'rust/crates/forge-server/src/product_store/schema.sql')
    assert not target.exists()
    assert not list(tmp_path.iterdir())
