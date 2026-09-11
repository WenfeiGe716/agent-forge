"""Deterministic workflow tests through the public Runtime Interface.

The adapter below records activity requests; it never calls a model, database,
tool, command, native UI, or filesystem workspace.
"""
import copy
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from forge.workflow_core import DevelopmentFlow


def turn_result(done=True, **extra):
    return {'done': done, 'elapsed_s': 0, 'result': {'summary': 'Fixture result'}, **extra}


class RuntimeFixture:
    def __init__(self, ticks, turns=None, *, strategy='autonomous'):
        self.ticks = ticks
        self.turn_results = turns or {}
        self.requests = []
        self.stages = []
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.on_wait = None
        self.on_turn = None
        self.check_results = {}
        self.settings = {
            'execution_strategy': strategy, 'entry_agent_id': 'lead',
            'planner_id': 'lead', 'reviewer_id': 'worker',
            'runtime': {'max_parallel': 2, 'task_timeout_s': 100, 'command_timeout_s': 2},
            'agents': {aid: {'role': 'developer', 'runtime': {
                'task_timeout_s': 10, 'call_timeout_s': 5, 'command_timeout_s': 2,
            }} for aid in ['lead', 'worker']},
        }

    def start_time(self):
        return self.clock

    def now(self):
        return self.clock

    async def wait_condition(self, predicate):
        if not predicate():
            if not self.on_wait:
                raise AssertionError('Unexpected wait without a fixture human response')
            await self.on_wait(predicate)
        if not predicate():
            raise AssertionError('The supplied human response did not satisfy the wait')

    async def execute_activity(self, name, args, timeout):
        self.requests.append((name, copy.deepcopy(args), timeout))
        if name == 'prepare_run':
            return copy.deepcopy(self.settings)
        if name == 'set_stage':
            self.stages.append((args['status'], args.get('error', '')))
            return {'status': args['status']}
        if name == 'autonomy_tick':
            return copy.deepcopy(self.ticks[args['tick']])
        if name == 'turn':
            if self.on_turn:
                await self.on_turn(args)
            value = self.turn_results.get((args['key'], args['index']), turn_result())
            if isinstance(value, Exception):
                raise value
            self.clock += timedelta(seconds=value['elapsed_s'])
            return copy.deepcopy(value)
        if name == 'integrate_task':
            return {'integrated': True}
        if name == 'check_project':
            return self.check_results.get(args['round'], {'verification': 'unverified', 'commands': []})
        raise AssertionError('Unexpected activity: ' + name)

    def arguments(self, name):
        return [args for activity, args, _ in self.requests if activity == name]


class AutonomousRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_delegation_runs_before_coordinator_finishes_and_acceptance_is_human(self):
        rt = RuntimeFixture([
            {'runnable': [{'key': 'lead_0', 'agent_id': 'lead'}]},
            {'runnable': [{'key': 'work_0', 'agent_id': 'worker'}, {'key': 'lead_0', 'agent_id': 'lead'}]},
            {'integrate': ['work_0']},
            {'candidate': {'digest': 'evidence-v1'}},
        ], {('lead_0', 0): turn_result(False)})
        flow = DevelopmentFlow(rt)

        async def human_accepts(predicate):
            self.assertEqual(flow.state()['stage'], 'awaiting_acceptance')
            self.assertFalse(predicate())
            flow.decide({'command_id': 'human-1', 'phase': 'acceptance',
                         'digest': 'evidence-v1', 'approved': True})

        rt.on_wait = human_accepts
        result = await flow.run('fixture-run')
        self.assertEqual(result, {'status': 'completed'})
        self.assertEqual([(a['key'], a['index']) for a in rt.arguments('turn')],
                         [('lead_0', 0), ('work_0', 0), ('lead_0', 1)])
        self.assertEqual([a['tick'] for a in rt.arguments('autonomy_tick')], [0, 1, 2, 3])
        self.assertEqual([a['key'] for a in rt.arguments('integrate_task')], ['work_0'])
        self.assertNotIn('save_plan', [name for name, _, _ in rt.requests])

    async def test_only_requested_checks_run_and_failed_checks_return_to_coordinator(self):
        rt = RuntimeFixture([
            {'checks': {'round': 0}},
            {'runnable': [{'key': 'inspect_failure', 'agent_id': 'lead'}]},
            {'checks': {'round': 1}},
            {'candidate': {'digest': 'checked-v2'}},
        ])
        rt.check_results = {0: {'verification': 'failed'}, 1: {'verification': 'passed'}}
        flow = DevelopmentFlow(rt)

        async def human_accepts(_predicate):
            flow.decide({'command_id': 'human-2', 'phase': 'acceptance',
                         'digest': 'checked-v2', 'approved': True})

        rt.on_wait = human_accepts
        self.assertEqual(await flow.run('fixture-run'), {'status': 'completed'})
        self.assertEqual([args['round'] for args in rt.arguments('check_project')], [0, 1])
        self.assertEqual([args['key'] for args in rt.arguments('turn')], ['inspect_failure'])
        self.assertNotIn('prepare_rework', [name for name, _, _ in rt.requests])

    async def test_approval_blocks_only_its_task_and_resumes_without_replaying_turn(self):
        rt = RuntimeFixture([
            {'runnable': [{'key': 'install', 'agent_id': 'lead'}, {'key': 'write', 'agent_id': 'worker'}]},
            {'blocked': [{'key': 'install', 'request_id': 'permit-1'}],
             'runnable': [{'key': 'write', 'agent_id': 'worker'}]},
            {'blocked': [{'key': 'install', 'request_id': 'permit-1'}]},
            {'runnable': [{'key': 'install', 'agent_id': 'lead'}]},
            {'stop': {'reason': 'Fixture has exercised the approved installation continuation'}},
        ], {('install', 0): turn_result(False, blocked_on='permit-1'),
            ('write', 0): turn_result(False)})
        flow = DevelopmentFlow(rt)

        async def approve(predicate):
            self.assertEqual(flow.stage, 'awaiting_decision')
            self.assertIn({'run_id': 'fixture-run', 'key': 'write', 'index': 1}, rt.arguments('turn'))
            self.assertFalse(predicate())
            rt.clock += timedelta(seconds=1000)
            flow.respond({'command_id': 'approval-1', 'request_id': 'permit-1', 'rejected': False})

        rt.on_wait = approve
        self.assertEqual(await flow.run('fixture-run'), {'status': 'needs_attention'})
        self.assertEqual([a['index'] for a in rt.arguments('turn') if a['key'] == 'install'], [0, 1])
        self.assertEqual(rt.arguments('autonomy_tick')[3]['answered'], ['permit-1'])
        self.assertEqual(rt.stages[-1][1], 'Fixture has exercised the approved installation continuation')

    async def test_rejected_permission_never_becomes_an_answered_grant(self):
        rt = RuntimeFixture([{'blocked': [{'key': 'install', 'request_id': 'permit-1'}]}])
        flow = DevelopmentFlow(rt)

        async def reject(_predicate):
            flow.respond({'command_id': 'deny-1', 'request_id': 'permit-1', 'rejected': True})

        rt.on_wait = reject
        self.assertEqual(await flow.run('fixture-run'), {'status': 'needs_attention'})
        self.assertIn('用户拒绝', rt.stages[-1][1])
        self.assertEqual(rt.arguments('turn'), [])
        self.assertEqual(len(rt.arguments('autonomy_tick')), 1)

    async def test_batch_budget_preflight_starts_no_peer_when_one_task_is_exhausted(self):
        rt = RuntimeFixture([
            {'runnable': [{'key': 'lead_0', 'agent_id': 'lead'}]},
            {'runnable': [{'key': 'work_0', 'agent_id': 'worker'}, {'key': 'lead_0', 'agent_id': 'lead'}]},
        ], {('lead_0', 0): turn_result(False, elapsed_s=10)})
        flow = DevelopmentFlow(rt)
        self.assertEqual(await flow.run('fixture-run'), {'status': 'needs_attention'})
        self.assertIn('Agent任务超过自动执行时限', rt.stages[-1][1])
        self.assertEqual([a['key'] for a in rt.arguments('turn')], ['lead_0'])

    async def test_duplicate_activation_is_rejected_before_any_turn_starts(self):
        rt = RuntimeFixture([{'runnable': [
            {'key': 'work_0', 'agent_id': 'worker'}, {'key': 'work_0', 'agent_id': 'worker'},
        ]}])
        flow = DevelopmentFlow(rt)
        self.assertEqual(await flow.run('fixture-run'), {'status': 'needs_attention'})
        self.assertEqual(rt.arguments('turn'), [])
        self.assertIn('重复', rt.stages[-1][1])

    async def test_cancel_and_failure_join_in_flight_peers_before_terminal_state(self):
        for fail in [False, True]:
            with self.subTest(fail=fail):
                rt = RuntimeFixture([{'runnable': [
                    {'key': 'lead_0', 'agent_id': 'lead'}, {'key': 'work_0', 'agent_id': 'worker'},
                ]}], {('lead_0', 0): RuntimeError('uncertain execution') if fail else turn_result()})
                flow = DevelopmentFlow(rt)
                peer_finished = asyncio.Event()

                async def control_during_turn(args):
                    if args['key'] == 'lead_0':
                        if not fail:
                            flow.control({'command_id': 'cancel-1', 'action': 'cancel'})
                    else:
                        await asyncio.sleep(0)
                        self.assertEqual(flow.stage, 'running')
                        peer_finished.set()

                rt.on_turn = control_during_turn
                self.assertEqual(await flow.run('fixture-run'),
                                 {'status': 'needs_attention' if fail else 'cancelled'})
                self.assertTrue(peer_finished.is_set())
                self.assertEqual(len(rt.arguments('turn')), 2)
                self.assertEqual(len(rt.arguments('autonomy_tick')), 1)

    async def test_pause_at_join_boundary_resumes_next_turn_not_previous_side_effect(self):
        rt = RuntimeFixture([
            {'runnable': [{'key': 'work_0', 'agent_id': 'worker'}]},
            {'runnable': [{'key': 'work_0', 'agent_id': 'worker'}]},
            {'stop': {'reason': 'Fixture finished pause verification'}},
        ], {('work_0', 0): turn_result(False)})
        flow = DevelopmentFlow(rt)

        async def pause(args):
            if args['index'] == 0:
                flow.control({'command_id': 'pause-1', 'action': 'pause'})

        async def resume(_predicate):
            self.assertEqual(flow.stage, 'paused')
            self.assertEqual(len(rt.arguments('turn')), 1)
            rt.clock += timedelta(seconds=1000)
            flow.control({'command_id': 'resume-1', 'action': 'resume'})

        rt.on_turn, rt.on_wait = pause, resume
        self.assertEqual(await flow.run('fixture-run'), {'status': 'needs_attention'})
        self.assertEqual([a['index'] for a in rt.arguments('turn')], [0, 1])
        self.assertEqual(rt.stages[-1][1], 'Fixture finished pause verification')

    async def test_a_candidate_does_not_skip_a_pending_permission(self):
        rt = RuntimeFixture([{'blocked': [{'key': 'work_0', 'request_id': 'permit-1'}],
                              'candidate': {'digest': 'not-yet-eligible'}}])
        flow = DevelopmentFlow(rt)

        async def reject(_predicate):
            self.assertEqual(flow.stage, 'awaiting_decision')
            flow.respond({'command_id': 'deny-1', 'request_id': 'permit-1', 'rejected': True})

        rt.on_wait = reject
        self.assertEqual(await flow.run('fixture-run'), {'status': 'needs_attention'})
        self.assertNotIn('awaiting_acceptance', [status for status, _ in rt.stages])

    async def test_rejected_or_wrong_version_acceptance_never_marks_completed(self):
        rt = RuntimeFixture([{'candidate': {'digest': 'current-evidence'}}])
        flow = DevelopmentFlow(rt)

        async def reject_current(predicate):
            flow.decide({'command_id': 'stale', 'phase': 'acceptance',
                         'digest': 'previous-evidence', 'approved': True})
            self.assertFalse(predicate())
            flow.decide({'command_id': 'current', 'phase': 'acceptance',
                         'digest': 'current-evidence', 'approved': False})

        rt.on_wait = reject_current
        self.assertEqual(await flow.run('fixture-run'), {'status': 'needs_attention'})
        self.assertEqual(rt.stages[-1][1], '人工验收未通过')
        self.assertNotIn('completed', [status for status, _ in rt.stages])

    async def test_quiescence_without_evidence_is_not_success(self):
        rt = RuntimeFixture([{}])
        self.assertEqual(await DevelopmentFlow(rt).run('fixture-run'), {'status': 'needs_attention'})
        self.assertIn('未判为完成', rt.stages[-1][1])

    async def test_cancel_while_awaiting_permission_does_not_execute_another_turn(self):
        rt = RuntimeFixture([{'blocked': [{'key': 'work_0', 'request_id': 'permit-1'}]}])
        flow = DevelopmentFlow(rt)

        async def cancel(_predicate):
            flow.control({'command_id': 'cancel', 'action': 'cancel'})

        rt.on_wait = cancel
        self.assertEqual(await flow.run('fixture-run'), {'status': 'cancelled'})
        self.assertEqual(rt.arguments('turn'), [])

    async def test_global_execution_timeout_still_applies(self):
        rt = RuntimeFixture([{'runnable': [{'key': 'work_0', 'agent_id': 'worker'}]}],
                            {('work_0', 0): turn_result(False, elapsed_s=101)})
        self.assertEqual(await DevelopmentFlow(rt).run('fixture-run'), {'status': 'needs_attention'})
        self.assertEqual(len(rt.arguments('turn')), 1)
        self.assertIn('超过本次运行的自动执行时限', rt.stages[-1][1])

    async def test_reconciled_coordinator_wakeup_advances_to_a_fresh_tick(self):
        rt = RuntimeFixture([
            {'again': True, 'runnable': [], 'blocked': []},
            {'runnable': [{'key': 'lead_feedback', 'agent_id': 'lead'}]},
            {'stop': {'reason': 'Fixture observed coordinator feedback'}},
        ])
        self.assertEqual(await DevelopmentFlow(rt).run('fixture-run'), {'status': 'needs_attention'})
        self.assertEqual([a['key'] for a in rt.arguments('turn')], ['lead_feedback'])
        self.assertEqual([a['tick'] for a in rt.arguments('autonomy_tick')], [0, 1, 2])
        self.assertEqual(rt.stages[-1][1], 'Fixture observed coordinator feedback')


if __name__ == '__main__':
    unittest.main()
