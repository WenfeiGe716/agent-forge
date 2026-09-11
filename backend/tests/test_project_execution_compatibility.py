import copy

from forge.db import Project, session


def body(project):
    return {k:v for k,v in project.items() if k not in ('id','owner_id','base_commit','created_at')}


def test_old_metadata_update_does_not_enable_host_execution(client,configured):
    project=configured['project']
    with session() as s:
        row=s.get(Project,project['id']);config=copy.deepcopy(row.config)
        config.pop('execution_mode',None);config.pop('permission_profile',None);row.config=config
    data=body(project);data.pop('execution_mode',None);data.pop('permission_profile',None)
    data['name']='Legacy name edit'
    response=client.put('/api/projects/'+project['id'],json=data)
    assert response.status_code==200,response.text
    assert response.json()['execution_mode']=='container'
    assert response.json()['permission_profile']=='ask'


def test_full_access_requires_explicit_confirmation_again_on_edit(client,configured):
    project=configured['project'];data=body(project)
    data.update(execution_mode='local',permission_profile='full',full_access_confirmed=True)
    response=client.put('/api/projects/'+project['id'],json=data)
    assert response.status_code==200,response.text
    assert 'full_access_confirmed' not in response.json()
    edited=body(response.json());edited['name']='Just rename'
    assert client.put('/api/projects/'+project['id'],json=edited).status_code==422
    edited.pop('permission_profile');edited.pop('execution_mode')
    assert client.put('/api/projects/'+project['id'],json=edited).status_code==422


def test_explicit_local_ask_or_auto_is_saved_without_full_grant(client,configured):
    project=configured['project']
    for profile in ('ask','auto'):
        data=body(project);data.update(execution_mode='local',permission_profile=profile)
        response=client.put('/api/projects/'+project['id'],json=data)
        assert response.status_code==200,response.text
        assert response.json()['permission_profile']==profile
