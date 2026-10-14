"""
Forge Observability & HITL Tracer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Maintains an immutable execution graph of Agentic logic cycles
(Thought -> Action -> Observation) and handles Human-in-The-Loop
escalations for operations requiring approval.
"""
from __future__ import annotations

import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import Optional, Any

@dataclass
class TraceStep:
    id: str
    trace_id: str
    type: str          # "thought" | "tool_call" | "observation"
    content: str
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0

@dataclass
class AgentTrace:
    id: str
    session_id: str
    task: str
    steps: list[TraceStep] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    status: str = "running"   # running | hitl_blocked | completed | failed

class HITLManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending_approvals: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}

    def request_approval(self, action: str, details: str, model: str) -> bool:
        req_id = str(uuid.uuid4())[:8]
        event = threading.Event()
        
        with self._lock:
            self._pending_approvals[req_id] = {
                "action": action,
                "details": details,
                "model": model,
                "status": "pending",
                "timestamp": time.time()
            }
            self._events[req_id] = event
        print(f"\n[HITL] 🔥 Escalation required: {action}")
        print(f"[HITL] 🔥 Waiting for human approval via Dashboard (req: {req_id})...")
        
        # Block the current agent thread here until resolved
        event.wait(timeout=300) # 5m timeout
        
        with self._lock:
            if req_id not in self._pending_approvals:
                return False # Timed out or removed
                
            status = self._pending_approvals[req_id].get("status", "denied")
            del self._pending_approvals[req_id]
            del self._events[req_id]
        
        return status == "approved"

    def resolve(self, req_id: str, approved: bool):
        with self._lock:
            if req_id in self._pending_approvals:
                self._pending_approvals[req_id]["status"] = "approved" if approved else "denied"
                self._events[req_id].set()

    def get_pending(self) -> list[dict]:
        with self._lock:
            res = [{"id": rid, **req} for rid, req in self._pending_approvals.items()]
        return sorted(res, key=lambda x: x["timestamp"])

class ExecutionTracer:
    def __init__(self):
        self._lock = threading.Lock()
        self._traces: dict[str, AgentTrace] = {}
        self.hitl = HITLManager()
        
    def start_trace(self, session_id: str, task: str) -> AgentTrace:
        tid = str(uuid.uuid4())[:8]
        trace = AgentTrace(id=tid, session_id=session_id, task=task)
        with self._lock:
            self._traces[tid] = trace
        return trace
        
    def add_step(self, trace_id: str, step_type: str, content: str, 
                 tool_name: str = None, tool_args: dict = None) -> TraceStep:
        with self._lock:
            if trace_id not in self._traces:
                raise KeyError(f"Trace {trace_id} not found")
            step = TraceStep(id=str(uuid.uuid4())[:8], trace_id=trace_id, type=step_type, 
                             content=content, tool_name=tool_name, tool_args=tool_args)
            self._traces[trace_id].steps.append(step)
        return step
        
    def finish_trace(self, trace_id: str, status: str = "completed"):
        with self._lock:
            if trace_id in self._traces:
                self._traces[trace_id].status = status
                self._traces[trace_id].end_time = time.time()
            
    def get_trace(self, trace_id: str) -> Optional[AgentTrace]:
        with self._lock:
            return self._traces.get(trace_id)
        
    def get_all_traces(self) -> list[AgentTrace]:
        with self._lock:
            traces = list(self._traces.values())
        return sorted(traces, key=lambda x: x.start_time, reverse=True)

_TRACER_INSTANCE = None
def get_tracer() -> ExecutionTracer:
    global _TRACER_INSTANCE
    if _TRACER_INSTANCE is None:
        _TRACER_INSTANCE = ExecutionTracer()
    return _TRACER_INSTANCE
