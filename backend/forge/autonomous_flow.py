"""Deterministic autonomous scheduling over the existing Runtime Interface.

Activities own durable task/message reconciliation and evidence verification.
This module only advances their runnable work; no model, database, or filesystem
access is performed inside either the local or Temporal workflow.
"""
import asyncio


def _answered(flow):
    if any(answer.get('rejected') for answer in flow.human_answers.values()):
        raise RuntimeError('用户拒绝决策请求，保留现场并转人工')
    return sorted(flow.human_answers)


async def _advance_batch(flow, assignments):
    """Join ONE turn per task before observing new delegations or control signals."""
    keys = [item['key'] for item in assignments]
    agents = [item['agent_id'] for item in assignments]
    if len(set(keys)) != len(keys) or len(set(agents)) != len(agents):
        raise RuntimeError('自治调度返回重复任务或同一成员的并行激活，未执行')
    requests = []
    for item in assignments:
        key, agent_id = item['key'], item['agent_id']
        if agent_id not in flow.settings['agents']:
            raise RuntimeError('自治调度包含本次授权团队之外的成员，未执行')
        runtime = flow.settings['agents'][agent_id]['runtime']
        if flow.spent.get(key, 0) >= runtime['task_timeout_s']:
            raise RuntimeError('Agent任务超过自动执行时限')
        timeout = runtime['call_timeout_s'] * 6 + (runtime['command_timeout_s'] + 120) * 8 + 60
        requests.append(({'key': key, 'index': flow.turns.get(key, 0)}, timeout))
    # Preflight the whole batch before even constructing coroutines. An invalid
    # later assignment must not start a peer or leak an unawaited coroutine.
    outcomes = await asyncio.gather(*(flow.call('turn', args, timeout)
                                      for args, timeout in requests), return_exceptions=True)
    # A failed peer must never leave another side-effectful turn running while
    # the run already claims to be paused/stopped.
    for item, result in zip(assignments, outcomes):
        if isinstance(result, BaseException):
            continue
        key = item['key']
        flow.turns[key] = flow.turns.get(key, 0) + 1
        flow.spent[key] = flow.spent.get(key, 0) + result['elapsed_s']
    errors = [result for result in outcomes if isinstance(result, BaseException)]
    if errors:
        if getattr(flow.runtime, 'autonomous_recovery', False):
            failures = [{'key': item['key'], 'error': str(result)}
                        for item, result in zip(assignments, outcomes)
                        if isinstance(result, BaseException)]
            await flow.call('record_autonomous_failure', {'failures': failures})
            return
        raise errors[0]


async def run_autonomous(flow):
    tick = 0
    await flow.status('running')
    while True:
        await flow.gate()
        state = await flow.call('autonomy_tick', {'tick': tick, 'answered': _answered(flow)})
        tick += 1
        await flow.gate()
        if state.get('stop'):
            await flow.status('needs_attention', state['stop']['reason'])
            return {'status': flow.stage}
        if state.get('integrate'):
            for key in state['integrate']:
                await flow.gate()
                await flow.call('integrate_task', {'key': key})
            continue
        if state.get('checks'):
            await flow.status('verifying')
            timeout = (flow.settings['runtime']['command_timeout_s'] + 120) * 20 + 120
            await flow.call('check_project', {'round': state['checks']['round']}, timeout)
            await flow.gate()
            await flow.status('running')
            # The next durable tick evaluates actual receipts and may wake the
            # coordinator. A command failure is not a hard-coded rework plan.
            continue
        runnable = state.get('runnable', [])
        if runnable:
            await _advance_batch(flow, runnable[:flow.settings['runtime']['max_parallel']])
            continue
        if state.get('blocked'):
            requests = {item['request_id'] for item in state['blocked']}
            await flow.status('awaiting_decision')
            before = flow.runtime.now()
            await flow.runtime.wait_condition(
                lambda: flow.cancelled or flow.paused or any(rid in flow.human_answers for rid in requests))
            flow.wait_seconds += (flow.runtime.now() - before).total_seconds()
            await flow.gate()
            _answered(flow)
            await flow.status('running')
            continue
        if state.get('candidate'):
            await flow.status('awaiting_acceptance')
            accepted = await flow.wait_decision('acceptance', state['candidate']['digest'])
            await flow.status('completed' if accepted else 'needs_attention',
                              '' if accepted else '人工验收未通过')
            return {'status': flow.stage}
        if state.get('again'):
            # Reconciliation created a bounded new activation (e.g. check
            # feedback); observe it under a new durable identity, not this
            # tick's cached result. The normal gate still runs before polling.
            continue
        raise RuntimeError('团队暂无可执行任务或有效交付，请检查负责人状态；未判为完成')
