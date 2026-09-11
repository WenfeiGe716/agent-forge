"""Read-only migration comparison: reports counts/equality, never secrets or row data."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from data_store import _snapshot_database


def compare(source_db, desktop_db):
    report = {}
    with tempfile.TemporaryDirectory(prefix='forge-migration-check-') as directory:
        root = Path(directory)
        source, verify_source = _snapshot_database(source_db, root)
        target, verify_target = _snapshot_database(desktop_db, root)
        with closing(sqlite3.connect(source)) as left, closing(sqlite3.connect(target)) as right:
            tables = {r[0] for r in left.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # Session/desktop login audit changes are expected after first launch.
            for table in sorted(tables - {'sessions', 'events', 'sqlite_sequence'}):
                if not table.replace('_', '').isalnum(): continue
                original = sorted(left.execute('SELECT * FROM "' + table + '"').fetchall(), key=repr)
                copied = sorted(right.execute('SELECT * FROM "' + table + '"').fetchall(), key=repr)
                report[table] = {'source_count':len(original), 'desktop_count':len(copied), 'equal':original==copied}
        verify_source(); verify_target()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_db', type=Path)
    parser.add_argument('desktop_db', type=Path)
    args = parser.parse_args()
    result = compare(args.source_db.resolve(), args.desktop_db.resolve())
    print(json.dumps({'all_equal':all(x['equal'] for x in result.values()), 'tables':result}, ensure_ascii=False, indent=2))
