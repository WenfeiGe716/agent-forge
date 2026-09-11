import asyncio
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker
from .config import settings
from .db import init_db
from .workflows import DevelopmentWorkflow
from . import engine

ACTIVITIES = []
for name in ['prepare_run','set_stage','prepare_task','save_plan','integrate_task','prepare_review','prepare_rework','publish','prepare_discussion','finish_discussion','publish_discussion','prepare_goal_review','assess_goal','checkpoint_interrupted','open_meeting','prepare_action_plan','save_action_plan']:
    def wrap(fn, activity_name):
        async def invoke(data: dict) -> dict:
            return await asyncio.to_thread(fn, data)
        invoke.__name__ = activity_name
        return activity.defn(name=activity_name)(invoke)
    ACTIVITIES.append(wrap(getattr(engine, name), name))
for name in ['turn', 'check_project']:
    ACTIVITIES.append(activity.defn(name=name)(getattr(engine, name)))

@activity.defn(name='autonomy_tick')
async def autonomy_tick(data: dict) -> dict:
    from .autonomy import autonomy_tick as reconcile
    return await asyncio.to_thread(reconcile, data)

ACTIVITIES.append(autonomy_tick)

async def main():
    init_db()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    async with Worker(client, task_queue=settings.task_queue, workflows=[DevelopmentWorkflow],
                      activities=ACTIVITIES, max_concurrent_activities=settings.max_active_activities):
        await asyncio.Event().wait()

if __name__ == '__main__': asyncio.run(main())
