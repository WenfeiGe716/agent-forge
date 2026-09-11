"""Shared workflow business rules. The selected runtime is the sole orchestrator per installation."""
import asyncio
from datetime import timedelta
class FlowError(RuntimeError):
    def __init__(self, message, **kwargs): super().__init__(message)

class DevelopmentFlow:
    def __init__(self, runtime):
        self.runtime = runtime
        self.paused = False
        self.cancelled = False
        self.seen = set()
        self.decisions = {}
        self.human_answers = {}
        self.stage = 'queued'
        self.wait_seconds = 0.0
        self.run_id = ''
        self.settings = {}
        self.turns = {}
        self.spent = {}

    def control(self, payload: dict):
        if payload['command_id'] in self.seen: return
        self.seen.add(payload['command_id'])
        action = payload['action']
        if action == 'cancel': self.cancelled = True
        elif action == 'pause': self.paused = True
        elif action == 'resume': self.paused = False

    def decide(self, payload: dict):
        if payload['command_id'] in self.seen: return
        self.seen.add(payload['command_id'])
        # A decision is scoped to the exact plan or artifact digest, never to "whatever is current".
        self.decisions.setdefault(payload['phase'] + ':' + payload['digest'], payload)

    def respond(self,payload):
        if payload['command_id'] in self.seen:return
        self.seen.add(payload['command_id'])
        self.human_answers.setdefault(payload['request_id'],payload)

    def state(self):
        return {'stage': self.stage, 'paused_requested': self.paused, 'cancel_requested': self.cancelled}

    async def call(self, name: str, args: dict, timeout=300):
        return await self.runtime.execute_activity(name, {'run_id': self.run_id, **args}, timeout)

    async def status(self, value, error=''):
        self.stage = value
        await self.call('set_stage', {'status': value, 'error': error})

    async def gate(self):
        if self.cancelled: raise FlowError('用户取消', type='UserCancelled', non_retryable=True)
        if self.paused:
            previous = self.stage
            # All parallel turns have joined before this gate: paused means no in-flight agent activity.
            await self.status('paused')
            before = self.runtime.now()
            await self.runtime.wait_condition(lambda: not self.paused or self.cancelled)
            self.wait_seconds += (self.runtime.now() - before).total_seconds()
            if self.cancelled: raise FlowError('用户取消', type='UserCancelled', non_retryable=True)
            await self.status(previous)
        if self.settings:
            elapsed = (self.runtime.now() - self.started).total_seconds() - self.wait_seconds
            if elapsed > self.settings['runtime']['task_timeout_s']:
                raise FlowError('超过本次运行的自动执行时限', non_retryable=True)

    async def wait_decision(self, phase, checksum):
        key = phase + ':' + checksum
        before = self.runtime.now()
        await self.runtime.wait_condition(lambda: key in self.decisions or self.cancelled)
        self.wait_seconds += (self.runtime.now() - before).total_seconds()
        await self.gate()
        return self.decisions[key]['approved']

    async def execute_tasks(self, assignments: list[tuple[str,str]]) -> dict:
        pending = dict(assignments)
        results = {}
        blocked = {}
        maximum = self.settings['runtime']['max_parallel']
        while pending:
            await self.gate()
            for key,rid in list(blocked.items()):
                if rid in self.human_answers:
                    if self.human_answers[rid].get('rejected'):raise FlowError('用户拒绝决策请求，保留现场并转人工')
                    del blocked[key]
            runnable=[(k,a) for k,a in pending.items() if k not in blocked]
            if not runnable:
                previous=self.stage
                await self.status('awaiting_decision')
                before=self.runtime.now()
                await self.runtime.wait_condition(lambda:self.cancelled or self.paused or any(rid in self.human_answers for rid in blocked.values()))
                self.wait_seconds+=(self.runtime.now()-before).total_seconds()
                await self.gate()
                await self.status(previous)
                continue
            batch = runnable[:maximum]
            calls = []
            for key, agent_id in batch:
                runtime = self.settings['agents'][agent_id]['runtime']
                if self.spent.get(key, 0) >= runtime['task_timeout_s']:
                    raise FlowError('Agent任务超过自动执行时限', non_retryable=True)
                timeout = runtime['call_timeout_s'] * 6 + (runtime['command_timeout_s'] + 120) * 8 + 60
                calls.append(self.call('turn', {'key': key, 'index': self.turns.get(key, 0)}, timeout))
            outcomes = await asyncio.gather(*calls, return_exceptions=True)
            # Join all in-flight activities before propagating errors or responding to pause/cancel.
            errors = [r for r in outcomes if isinstance(r, BaseException)]
            if errors: raise errors[0]
            for (key, agent_id), result in zip(batch, outcomes):
                self.turns[key] = self.turns.get(key, 0) + 1
                self.spent[key] = self.spent.get(key, 0) + result['elapsed_s']
                if result.get('blocked_on'):blocked[key]=result['blocked_on']
                if result['done']:
                    results[key] = result['result']
                    del pending[key]
        await self.gate()
        return results

    async def discuss(self, graph):
        await self.status('discussing')
        cycle = 0
        checkpoint = {}
        while True:
            await self.gate()
            cycle += 1  # Sequence identity only. It is never a completion condition.
            assignments = []
            for order, aid in enumerate(graph['participants']):
                key = f'discussion_{cycle}_{order}'
                await self.call('prepare_discussion', {'round': cycle, 'order': order, 'checkpoint': checkpoint})
                if graph['strategy'] == 'sequential':
                    await self.execute_tasks([(key, aid)])
                else:
                    assignments.append((key, aid))
            if assignments:
                await self.execute_tasks(assignments)
            await self.gate()
            review = await self.call('prepare_goal_review', {'cycle': cycle, 'checkpoint': checkpoint})
            await self.execute_tasks([(review['key'], review['agent_id'])])
            checkpoint = await self.call('assess_goal', {'cycle': cycle, 'checkpoint': checkpoint})
            if checkpoint['outcome'] != 'continue':
                discussion = await self.call('finish_discussion', {'rounds_completed': cycle,
                    'stop_reason': checkpoint['outcome'], 'checkpoint': checkpoint})
                if checkpoint['outcome'] == 'goal_satisfied':
                    return discussion
                await self.call('publish_discussion', {'discussion': discussion})
                raise FlowError('讨论缺少人工决策' if checkpoint['outcome']=='needs_human' else '目标持续无有效进展；保存部分成果并转人工，不判为完成')

    async def run(self, run_id: str) -> dict:
        self.run_id, self.started = run_id, self.runtime.start_time()
        try:
            self.settings = await self.call('prepare_run', {})
            if self.settings.get('execution_strategy') == 'autonomous':
                from .autonomous_flow import run_autonomous
                return await run_autonomous(self)
            graph=self.settings.get('orchestration')
            if self.settings.get('collaboration_flow'):
                await self.status('intake')
                await self.execute_tasks([('intake',self.settings['entry_agent_id'])])
            if graph and graph.get('participants'):
                graph=await self.call('open_meeting',{})
                discussion=await self.discuss(graph)
                if graph['mode']=='discussion':
                    if self.settings.get('collaboration_flow'):
                        await self.call('prepare_action_plan',{'discussion':discussion})
                        await self.execute_tasks([('action_plan',self.settings['entry_agent_id'])])
                        await self.call('save_action_plan',{})
                    artifact=await self.call('publish_discussion',{'discussion':discussion})
                    await self.status('awaiting_acceptance')
                    approved=await self.wait_decision('acceptance',artifact['digest'])
                    await self.status('completed' if approved else 'needs_attention','' if approved else '讨论成果未通过人工验收')
                    return {'status':self.stage}
            await self.status('planning')
            await self.execute_tasks([('plan', self.settings['planner_id'])])
            proposal = await self.call('save_plan', {})
            await self.status('awaiting_approval')
            if not await self.wait_decision('plan', proposal['digest']):
                await self.status('cancelled'); return {'status': 'cancelled'}
            await self.status('running')
            remaining = {t['key']: t for t in proposal['plan']['tasks']}
            done = set()
            while remaining:
                await self.gate()
                ready = [t for t in remaining.values() if set(t['depends_on']) <= done]
                if not ready: raise FlowError('计划依赖无法满足', non_retryable=True)
                # Each wave starts from the same integrated snapshot; integration is serial.
                for item in ready: await self.call('prepare_task', {'key': item['key']})
                await self.execute_tasks([(t['key'], t['agent_id']) for t in ready])
                for item in ready:
                    await self.gate()
                    await self.call('integrate_task', {'key': item['key']})
                    done.add(item['key']); del remaining[item['key']]
            maximum = self.settings['runtime']['max_rework_rounds']
            for round_no in range(maximum + 1):
                await self.gate()
                await self.status('verifying')
                timeout = (self.settings['runtime']['command_timeout_s'] + 120) * 20 + 120
                checks = await self.call('check_project', {'round': round_no}, timeout)
                await self.call('prepare_review', {'round': round_no, 'checks': checks})
                await self.status('reviewing')
                key = f'review_{round_no}'
                review = (await self.execute_tasks([(key, self.settings['reviewer_id'])]))[key]
                passed = review['verdict'] == 'pass' and checks['verification'] != 'failed'
                if passed or round_no == maximum: break
                await self.status('reworking')
                key = f'rework_{round_no + 1}'
                await self.call('prepare_rework', {'round': round_no + 1, 'feedback': {'checks': checks, 'review': review}})
                developer = next(k for k,a in self.settings['agents'].items() if a['role'] in ('developer','tester'))
                await self.execute_tasks([(key, developer)])
                await self.call('integrate_task', {'key': key})
            artifact = await self.call('publish', {'checks': checks, 'review': review})
            if not passed:
                await self.status('needs_attention', '达到返工上限，验收或审查仍未通过；已保留成果')
                return {'status': 'needs_attention'}
            await self.status('awaiting_acceptance')
            approved = await self.wait_decision('acceptance', artifact['digest'])
            await self.status('completed' if approved else 'needs_attention', '' if approved else '人工验收未通过')
            return {'status': self.stage}
        except Exception as exc:
            if self.cancelled:
                await self.status('cancelled')
            else:
                cause = exc
                while getattr(cause, 'cause', None): cause = cause.cause
                if (self.settings.get('orchestration') or {}).get('participants'):
                    try:
                        text = str(cause)
                        reason = 'budget_exhausted' if '调用次数上限' in text else 'timeout' if '时限' in text else 'interrupted'
                        # A completed checkpoint already records stalled/needs_human precisely.
                        if '持续无有效进展' not in text and '缺少人工决策' not in text:
                            await self.call('checkpoint_interrupted', {'reason': reason, 'error': text[:2000]})
                    except Exception:
                        pass  # Never mask the primary uncertain/failed execution result.
                await self.status('needs_attention', str(cause)[:2200])
            return {'status': self.stage}
