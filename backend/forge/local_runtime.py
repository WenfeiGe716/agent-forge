"""Optional single-process durable local runner; never executes shell commands on host.

SQLite journal + a process lock provide restart-safe *completed* activity reuse.
An activity left started by an interruption is UNKNOWN and stops for human inspection.
This is not a distributed/high-availability replacement for Temporal.
"""
import asyncio
import hashlib
import inspect
import json
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from .config import settings
from .db import session, Run, Outbox, ActivityJournal
from .workflow_core import DevelopmentFlow
from . import engine

EPOCH = datetime(2000,1,1,tzinfo=timezone.utc)
TERMINAL = {'completed','cancelled','needs_attention'}
ACTIVITIES = {'prepare_run','set_stage','prepare_task','save_plan','integrate_task','prepare_review','prepare_rework','publish','turn','check_project','prepare_discussion','finish_discussion','publish_discussion','prepare_goal_review','assess_goal','checkpoint_interrupted','open_meeting','prepare_action_plan','save_action_plan','autonomy_tick'}

class LocalRuntime:
    def __init__(self, run_id, semaphore):
        self.run_id, self.semaphore = run_id, semaphore
        with session() as s:
            self.elapsed = sum(float(x.elapsed_s) for x in s.scalars(select(ActivityJournal).where(ActivityJournal.run_id==run_id,ActivityJournal.state=='completed')))
    def now(self): return EPOCH + timedelta(seconds=self.elapsed)
    def start_time(self): return EPOCH
    async def wait_condition(self, predicate):
        while not predicate(): await asyncio.sleep(.05)
    async def execute_activity(self, name, args, timeout):
        if name not in ACTIVITIES: raise ValueError('未知活动')
        # Status writes are idempotent and reflect this replay's current boundary.
        # Every side-effectful operation has a deterministic identity.
        # Local replay preloads durable human answers, including answers that
        # arrived AFTER an earlier tick. Reuse that tick's original result;
        # only a new sequence identity may observe the new input. Other activity
        # identities retain all arguments, including command/permission scope.
        identity = {'run_id': args['run_id'], 'tick': args['tick']} if name == 'autonomy_tick' else args
        operation = hashlib.sha256(json.dumps([name,identity],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if name != 'set_stage':
            with session() as s:
                old = s.get(ActivityJournal, operation)
                if old:
                    if old.state == 'completed': return old.result
                    raise RuntimeError(f'活动 {name} 的先前执行结果未知，禁止自动重放；请检查现场后创建新运行')
                s.add(ActivityJournal(id=operation,run_id=self.run_id,name=name,state='started',result={}))
        async with self.semaphore:
            started=time.monotonic()
            if name == 'autonomy_tick':
                from .autonomy import autonomy_tick
                fn = autonomy_tick
            else:
                fn=getattr(engine,name)
            # The provider/tool routines enforce their own timeouts. Do not cancel a
            # to_thread filesystem operation and pretend it stopped.
            result=await fn(args) if inspect.iscoroutinefunction(fn) else await asyncio.to_thread(fn,args)
            elapsed=time.monotonic()-started
            self.elapsed+=elapsed
            if name != 'set_stage':
                with session() as s:
                    row=s.get(ActivityJournal,operation)
                    row.state,row.result,row.elapsed_s='completed',result,str(elapsed)
            return result

class LocalScheduler:
    def __init__(self):
        self.running={}
        self.semaphore=asyncio.Semaphore(settings.max_active_activities)
        self.lock=None
    async def serve(self, app):
        import fcntl
        settings.data_root.mkdir(parents=True,exist_ok=True)
        self.lock=open(settings.data_root/'local-runner.lock','a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            app.state.dispatch_status='unavailable: another local runner owns the process lock'
            raise RuntimeError('本地模式仅允许一个API进程；发现另一个运行器，拒绝重复执行')
        app.state.dispatch_status='connected (local journal)'
        try:
            while True:
                with session() as s:
                    candidates=list(s.scalars(select(Run).where(Run.status.notin_(TERMINAL)).order_by(Run.created_at)))
                for run in candidates:
                    if run.id in self.running: continue
                    # Limit total active runs; awaiting approvals consumes one slot.
                    if len(self.running)>=settings.max_active_runs: break
                    runtime=LocalRuntime(run.id,self.semaphore)
                    flow=DevelopmentFlow(runtime)
                    # All prior durable controls/decisions reconstruct the latest intent.
                    with session() as s:
                        commands=list(s.scalars(select(Outbox).where(Outbox.run_id==run.id).order_by(Outbox.created_at,Outbox.id)))
                    for m in commands:
                        if m.action=='control': flow.control({**m.payload,'command_id':m.id})
                        elif m.action=='decide': flow.decide({**m.payload,'command_id':m.id})
                        elif m.action=='respond': flow.respond({**m.payload,'command_id':m.id})
                    job=asyncio.create_task(flow.run(run.id))
                    self.running[run.id]=(flow,job)
                with session() as s:
                    commands=list(s.scalars(select(Outbox).where(Outbox.sent==False).order_by(Outbox.created_at,Outbox.id)))
                for m in commands:
                    if m.run_id not in self.running: continue
                    flow,job=self.running[m.run_id]
                    if m.action=='control': flow.control({**m.payload,'command_id':m.id})
                    elif m.action=='decide': flow.decide({**m.payload,'command_id':m.id})
                    elif m.action=='respond': flow.respond({**m.payload,'command_id':m.id})
                    with session() as s:
                        row=s.get(Outbox,m.id);row.sent,row.error=True,''
                for rid,(flow,job) in list(self.running.items()):
                    if job.done():
                        try: job.result()
                        except Exception as exc:
                            await asyncio.to_thread(engine.set_stage,{'run_id':rid,'status':'needs_attention','error':f'本地编排异常：{type(exc).__name__}: {exc}'[:2000]})
                        del self.running[rid]
                await asyncio.sleep(.15)
        finally:
            # Interruptions leave started journal records UNKNOWN. Never clear them.
            jobs=[j for _,j in self.running.values()]
            for job in jobs: job.cancel()
            if jobs: await asyncio.gather(*jobs,return_exceptions=True)
            fcntl.flock(self.lock,fcntl.LOCK_UN);self.lock.close()
