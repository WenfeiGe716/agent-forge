"""Local journal / Temporal registration checks with an inert tick adapter.

conftest owns the isolated test database; the tick never executes models/tools.
"""
import asyncio
import sys
import types

import pytest

from forge.local_runtime import LocalRuntime
from forge.workflow_core import DevelopmentFlow


@pytest.mark.asyncio
async def test_tick_identity_survives_replayed_future_answers_and_advances_with_new_tick(monkeypatch):
    calls = []
    module = types.ModuleType('forge.autonomy')

    def tick(data):
        calls.append(data)
        return {'runnable': [], 'observed_answers': list(data['answered'])}

    module.autonomy_tick = tick
    monkeypatch.setitem(sys.modules, 'forge.autonomy', module)
    first = LocalRuntime('isolated-autonomy', asyncio.Semaphore(1))
    args = {'run_id': 'isolated-autonomy', 'tick': 0, 'answered': []}
    assert (await first.execute_activity('autonomy_tick', args, 30))['observed_answers'] == []
    replay = LocalRuntime('isolated-autonomy', asyncio.Semaphore(1))
    assert (await replay.execute_activity('autonomy_tick', {**args, 'answered': ['human-1']}, 30))['observed_answers'] == []
    assert (await replay.execute_activity('autonomy_tick', {**args, 'tick': 1, 'answered': ['human-1']}, 30))['observed_answers'] == ['human-1']
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unknown_tick_is_not_replayed_even_after_answer_arrives(monkeypatch):
    calls = []
    module = types.ModuleType('forge.autonomy')

    def tick(data):
        calls.append(data)
        raise RuntimeError('fixture uncertain tick')

    module.autonomy_tick = tick
    monkeypatch.setitem(sys.modules, 'forge.autonomy', module)
    args = {'run_id': 'isolated-autonomy', 'tick': 0, 'answered': []}
    with pytest.raises(RuntimeError, match='fixture uncertain tick'):
        await LocalRuntime('isolated-autonomy', asyncio.Semaphore(1)).execute_activity('autonomy_tick', args, 30)
    with pytest.raises(RuntimeError, match='先前执行结果未知'):
        await LocalRuntime('isolated-autonomy', asyncio.Semaphore(1)).execute_activity('autonomy_tick', {**args, 'answered': ['human-1']}, 30)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_whole_local_flow_replays_prior_turns_with_preloaded_human_answers(monkeypatch):
    from forge import engine
    requests = []
    module = types.ModuleType('forge.autonomy')
    settings = {
        'execution_strategy': 'autonomous', 'entry_agent_id': 'lead',
        'runtime': {'max_parallel': 1, 'task_timeout_s': 100, 'command_timeout_s': 2},
        'agents': {'lead': {'runtime': {'task_timeout_s': 10, 'call_timeout_s': 5, 'command_timeout_s': 2}}},
    }

    def tick(data):
        requests.append(('tick', data['tick']))
        return [
            {'runnable': [{'key': 'lead_0', 'agent_id': 'lead'}]},
            {'blocked': [{'key': 'lead_0', 'request_id': 'human-1'}]},
            {'runnable': [{'key': 'lead_0', 'agent_id': 'lead'}]},
            {'candidate': {'digest': 'frozen-result'}},
        ][data['tick']]

    async def turn(data):
        requests.append(('turn', data['index']))
        result = {'done': data['index'] > 0, 'result': {'summary': 'Fixture'}, 'elapsed_s': 0}
        if data['index'] == 0:
            result['blocked_on'] = 'human-1'
        return result

    module.autonomy_tick = tick
    monkeypatch.setitem(sys.modules, 'forge.autonomy', module)
    monkeypatch.setattr(engine, 'prepare_run', lambda data: settings)
    monkeypatch.setattr(engine, 'set_stage', lambda data: {'status': data['status']})
    monkeypatch.setattr(engine, 'turn', turn)
    answer = {'command_id': 'answer-1', 'request_id': 'human-1', 'rejected': False}
    acceptance = {'command_id': 'accept-1', 'phase': 'acceptance', 'digest': 'frozen-result', 'approved': True}
    first = DevelopmentFlow(LocalRuntime('isolated-replay', asyncio.Semaphore(2)))

    async def respond(predicate):
        if first.stage == 'awaiting_decision':
            first.respond(answer)
        elif first.stage == 'awaiting_acceptance':
            first.decide(acceptance)
        assert predicate()

    first.runtime.wait_condition = respond
    assert await first.run('isolated-replay') == {'status': 'completed'}
    original = list(requests)
    replay = DevelopmentFlow(LocalRuntime('isolated-replay', asyncio.Semaphore(2)))
    replay.respond(answer)
    replay.decide(acceptance)
    assert await replay.run('isolated-replay') == {'status': 'completed'}
    assert requests == original
    assert requests == [('tick', 0), ('turn', 0), ('tick', 1), ('tick', 2), ('turn', 1), ('tick', 3)]


@pytest.mark.asyncio
async def test_temporal_worker_registers_the_same_tick_activity(monkeypatch):
    pytest.importorskip('temporalio', reason='Optional Temporal SDK is not installed; no Temporal integration claim')
    from forge import worker
    module = types.ModuleType('forge.autonomy')
    module.autonomy_tick = lambda data: {'tick': data['tick'], 'source': 'fixture'}
    monkeypatch.setitem(sys.modules, 'forge.autonomy', module)
    matches = [fn for fn in worker.ACTIVITIES if fn.__name__ == 'autonomy_tick']
    assert len(matches) == 1
    assert await matches[0]({'tick': 4}) == {'tick': 4, 'source': 'fixture'}
