import copy

from forge.config import settings
from forge.db import Project, session


def _project_body(project):
    return {
        key: value
        for key, value in project.items()
        if key not in {'id', 'owner_id', 'base_commit', 'created_at'}
    }


def _make_import_source(name='external-models'):
    source = settings.import_root / name
    source.mkdir(parents=True)
    (source / 'README.md').write_text('fixture\n')
    return name


def test_new_blank_and_imported_projects_allow_external_models_by_default(client):
    imported = client.post(
        '/api/projects',
        json={'name': 'Imported default', 'import_path': _make_import_source()},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()['allow_external_models'] is True

    blank = client.post('/api/projects', json={'name': 'Blank default', 'blank': True})
    assert blank.status_code == 201, blank.text
    assert blank.json()['allow_external_models'] is True


def test_new_project_explicitly_disables_external_models(client):
    imported = client.post(
        '/api/projects',
        json={
            'name': 'Imported opt out',
            'import_path': _make_import_source('external-models-opt-out'),
            'allow_external_models': False,
        },
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()['allow_external_models'] is False

    blank = client.post(
        '/api/projects',
        json={'name': 'Blank opt out', 'blank': True, 'allow_external_models': False},
    )
    assert blank.status_code == 201, blank.text
    assert blank.json()['allow_external_models'] is False


def test_project_update_omission_preserves_authorization_and_explicit_toggle(client):
    created = client.post(
        '/api/projects',
        json={'name': 'Authorization project', 'blank': True, 'allow_external_models': False},
    )
    assert created.status_code == 201, created.text
    project = created.json()

    omitted = _project_body(project)
    omitted.pop('allow_external_models')
    omitted['name'] = 'Authorization project renamed'
    response = client.put('/api/projects/' + project['id'], json=omitted)
    assert response.status_code == 200, response.text
    assert response.json()['allow_external_models'] is False

    toggled = _project_body(response.json())
    toggled['allow_external_models'] = True
    response = client.put('/api/projects/' + project['id'], json=toggled)
    assert response.status_code == 200, response.text
    assert response.json()['allow_external_models'] is True

    omitted = _project_body(response.json())
    omitted.pop('allow_external_models')
    response = client.put('/api/projects/' + project['id'], json=omitted)
    assert response.status_code == 200, response.text
    assert response.json()['allow_external_models'] is True

    toggled = _project_body(response.json())
    toggled['allow_external_models'] = False
    response = client.put('/api/projects/' + project['id'], json=toggled)
    assert response.status_code == 200, response.text
    assert response.json()['allow_external_models'] is False


def test_legacy_project_missing_authorization_key_stays_disabled_on_update(client):
    created = client.post('/api/projects', json={'name': 'Legacy project', 'blank': True})
    assert created.status_code == 201, created.text
    project = created.json()
    with session() as s:
        row = s.get(Project, project['id'])
        config = copy.deepcopy(row.config)
        config.pop('allow_external_models', None)
        row.config = config

    # Reading the project after the default change must not migrate its stored policy.
    listed = client.get('/api/projects')
    assert listed.status_code == 200, listed.text
    legacy = next(item for item in listed.json() if item['id'] == project['id'])
    assert legacy.get('allow_external_models', False) is False
    with session() as s:
        assert 'allow_external_models' not in s.get(Project, project['id']).config

    body = _project_body(project)
    body.pop('allow_external_models')
    body['name'] = 'Legacy project renamed'
    response = client.put('/api/projects/' + project['id'], json=body)
    assert response.status_code == 200, response.text
    assert response.json()['allow_external_models'] is False
