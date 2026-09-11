"""Read-only diagnostic: replay bulk-enable preview on a private DB snapshot.

Exit 0 means the actual preview accepted the selection; exit 1 reproduces a
configuration rejection. Does not load credentials, call models, or save state.
"""
import argparse
from pathlib import Path
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'desktop'), str(ROOT / 'backend')]
from data_store import _snapshot_database

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('database', type=Path)
parser.add_argument('--model-id', action='append', default=[])
parser.add_argument('--normalize-output-limit-in-memory', action='store_true',
                    help='Diagnostic only: rename max_tokens without committing any data')
args = parser.parse_args()

with tempfile.TemporaryDirectory(prefix='forge-bulk-diagnostic-') as temporary:
    snapshot, verify = _snapshot_database(args.database, Path(temporary))
    os.environ['DATABASE_URL'] = 'sqlite:///' + str(snapshot)
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from fastapi import HTTPException
    from forge.db import engine, ModelProfile, Provider, ProviderRevision
    from forge.provider_catalog import CatalogModelState
    from forge.provider_model_state import BulkStatePreview, _review

    failures = 0
    selected = 0
    with Session(engine, autoflush=False) as session:
        for provider in session.scalars(select(Provider)):
            rows = []
            for row in session.scalars(select(ModelProfile)):
                if row.config['provider_id'] != provider.id:
                    continue
                state = session.get(CatalogModelState, row.id)
                if state and state.retired:
                    continue
                if args.model_id and row.id not in args.model_id:
                    continue
                rows.append(row)
            if not rows:
                continue
            selected += len(rows)
            if args.normalize_output_limit_in_memory:
                connection = session.get(ProviderRevision, provider.revision_id)
                assert connection.config['protocol'] == 'openai_responses'
                for row in rows:
                    cfg = row.config
                    if 'max_tokens' not in cfg.get('allowed_parameters', []):
                        continue
                    assert 'max_output_tokens' not in cfg.get('parameters', {})
                    row.config = {**cfg,
                        'allowed_parameters': ['max_output_tokens' if key == 'max_tokens' else key
                                               for key in cfg['allowed_parameters']],
                        'parameters': {'max_output_tokens' if key == 'max_tokens' else key: value
                                       for key, value in cfg['parameters'].items()}}
            data = BulkStatePreview(revision_id=provider.revision_id, enabled=True,
                selections=[{'id': row.id, 'version': row.version} for row in rows])
            try:
                result = _review(session, provider.id, data)[2]
                print(f'PASS: {provider.name}: {result["counts"]["selected"]} configurations accepted')
            except (ValueError, HTTPException) as error:
                failures += 1
                print(f'FAIL: {provider.name}: {len(rows)} configurations: '
                      + (str(error) if isinstance(error, ValueError) else str(error.detail)))
        session.rollback()
    engine.dispose()
    verify()
    if not selected:
        print('FAIL: no matching current configuration selected')
    sys.exit(1 if failures or not selected else 0)
