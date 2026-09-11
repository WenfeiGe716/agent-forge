"""Model edits and catalog replacement must share their provider write boundary."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from test_upstream_model_pull import directory
from test_provider_full_replace import confirmation


@pytest.mark.parametrize('move_provider', [False, True], ids=['edit-same-provider', 'move-to-other-provider'])
def test_concurrent_model_edit_cannot_undo_retirement_or_reuse_a_history_version(client, configured, monkeypatch, move_provider):
    from forge import model_hub
    from forge.db import ModelProfile, session
    from forge.provider_catalog import CatalogModelState

    pid, catalog, _ = directory(client, configured, monkeypatch)
    mid = configured['models'][0]
    original = next(model for model in client.get('/api/models').json() if model['id'] == mid)
    body = {key: value for key, value in original.items() if key not in ('id', 'version')}
    body['name'] = 'Reviewed concurrent edit'
    if move_provider:
        body['provider_id'] = configured['providers'][1]

    edit_entered, release_edit, replace_started, replace_finished = Event(), Event(), Event(), Event()
    original_record = model_hub.record_model

    def pause_first_edit_record(s, model, actor):
        # The edit is started and observed here before replacement is submitted.
        # Other record_model calls (including replacement) are never paused.
        if model.id == mid and not edit_entered.is_set():
            edit_entered.set()
            if not release_edit.wait(5):
                raise AssertionError('Timed out waiting to release the isolated model edit')
        return original_record(s, model, actor)

    def replace():
        replace_started.set()
        try:
            return client.post(f'/api/providers/{pid}/catalog/replace', json=confirmation(catalog))
        finally:
            replace_finished.set()

    monkeypatch.setattr(model_hub, 'record_model', pause_first_edit_record)
    with ThreadPoolExecutor(max_workers=2) as pool:
        editing = pool.submit(client.put, '/api/models/' + mid, json=body)
        try:
            assert edit_entered.wait(5), 'Model edit did not reach its persistence boundary'
            replacing = pool.submit(replace)
            assert replace_started.wait(5)
            # Before the fix replacement completes against the old profile while
            # the edit is paused. With the shared lock it waits for this release.
            replacement_overtook_edit = replace_finished.wait(0.5)
        finally:
            release_edit.set()
        edited = editing.result(10)
        replaced = replacing.result(10)

    assert edited.status_code == 200, edited.text
    assert replaced.status_code == 201, replaced.text
    saved = next(model for model in client.get('/api/models').json() if model['id'] == mid)
    with session() as s:
        model = s.get(ModelProfile, mid)
        state = s.get(CatalogModelState, mid)
        retired_here = bool(state and state.retired and state.provider_id == model.config['provider_id'])
        assert not retired_here or model.config['enabled'] is False

    versions = client.get('/api/model-hub/models/' + mid + '/history').json()['versions']
    latest = next(version for version in versions if version['version'] == saved['version'])
    assert latest['config'] == {key: value for key, value in saved.items() if key not in ('id', 'version')}
    assert saved['name'] == body['name']
    if move_provider:
        assert saved['provider_id'] == configured['providers'][1]
        assert saved['enabled'] is True
        assert mid not in {model['id'] for model in replaced.json()['retired']}
    else:
        assert retired_here and saved['enabled'] is False
        assert mid in {model['id'] for model in replaced.json()['retired']}
    assert not replacement_overtook_edit, 'Replacement bypassed the in-progress model editor provider lock'
