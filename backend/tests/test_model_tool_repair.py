from forge.db import ModelProfile, PlatformConfig, session


def test_tool_test_promotes_only_the_checked_model_revision(client, configured, monkeypatch):
    model_id = configured['models'][0]
    with session() as s:
        model = s.get(ModelProfile, model_id)
        model.config = {**model.config, 'supports_tools': False}

    async def probe(connection, model, key):
        assert connection['protocol'] == 'openai_chat'
        assert model['model_id']
        assert key
        return {'checked_at': 'now', 'requests': 2, 'tool_roundtrip': True,
                'input_tokens': 4, 'output_tokens': 2}

    monkeypatch.setattr('forge.setup_api.probe_model_connection', probe)
    response = client.post(f'/api/models/{model_id}/tool-test')
    assert response.status_code == 200, response.text
    assert response.json()['supports_tools'] is True
    with session() as s:
        model = s.get(ModelProfile, model_id)
        probe_record = s.get(PlatformConfig, 'probe:' + model_id)
        assert model.config['supports_tools'] is True
        assert probe_record.value['revision_id']
        assert probe_record.value['model_version'] == model.version

