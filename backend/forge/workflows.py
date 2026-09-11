"""Temporal adapter. No provider/filesystem I/O occurs inside deterministic workflows."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from .workflow_core import DevelopmentFlow

class TemporalRuntime:
    def now(self): return workflow.now()
    def start_time(self): return workflow.now()
    async def wait_condition(self, predicate): await workflow.wait_condition(predicate)
    async def execute_activity(self, name, args, timeout):
        return await workflow.execute_activity(name, args, start_to_close_timeout=timedelta(seconds=timeout), retry_policy=RetryPolicy(maximum_attempts=1))

@workflow.defn
class DevelopmentWorkflow(DevelopmentFlow):
    def __init__(self): super().__init__(TemporalRuntime())
    @workflow.signal
    def control(self, payload: dict): super().control(payload)
    @workflow.signal
    def decide(self, payload: dict): super().decide(payload)
    @workflow.signal
    def respond(self,payload:dict): super().respond(payload)
    @workflow.query
    def state(self): return super().state()
    @workflow.run
    async def run(self, run_id: str) -> dict: return await super().run(run_id)
