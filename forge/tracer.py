"""
Forge Observability & HITL Tracer
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Maintains an immutable execution graph of Agentic logic cycles
(Thought -> Action -> Observation) and handles Human-in-The-Loop
escalations for operations requiring approval.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


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

    def to_dict(self) -> dict:
        return asdict(self)


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
    def __init__(self, tracer: Optional["ExecutionTracer"] = None):
        self._lock = threading.Lock()
        self._pending_approvals: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._tracer = tracer

    def request_approval(
        self,
        action: str,
        details: str,
        model: str,
        trace_id: Optional[str] = None,
        timeout: float = 300.0,
    ) -> bool:
        req_id = str(uuid.uuid4())[:8]
        event = threading.Event()

        # Mark active trace(s) as hitl_blocked while waiting
        blocked_traces: list[str] = []
        if self._tracer is not None:
            with self._tracer._lock:
                if trace_id and trace_id in self._tracer._traces:
                    self._tracer._traces[trace_id].status = "hitl_blocked"
                    blocked_traces.append(trace_id)
                else:
                    for tid, tr in self._tracer._traces.items():
                        if tr.status == "running":
                            tr.status = "hitl_blocked"
                            blocked_traces.append(tid)

        with self._lock:
            self._pending_approvals[req_id] = {
                "action": action,
                "details": details,
                "model": model,
                "trace_id": trace_id or (blocked_traces[0] if blocked_traces else ""),
                "status": "pending",
                "timestamp": time.time(),
            }
            self._events[req_id] = event
        print(f"\n[HITL] 🔥 Escalation required: {action}")
        print(f"[HITL] 🔥 Waiting for human approval via Dashboard (req: {req_id})...")

        # Block the current agent thread here until resolved
        event.wait(timeout=timeout)

        with self._lock:
            if req_id not in self._pending_approvals:
                approved = False
            else:
                status = self._pending_approvals[req_id].get("status", "denied")
                del self._pending_approvals[req_id]
                del self._events[req_id]
                approved = status == "approved"

        # Restore trace status to running if it was blocked by this request
        if self._tracer is not None and blocked_traces:
            with self._tracer._lock:
                for tid in blocked_traces:
                    if tid in self._tracer._traces and self._tracer._traces[tid].status == "hitl_blocked":
                        self._tracer._traces[tid].status = "running"

        return approved

    def resolve(self, req_id: str, approved: bool) -> bool:
        with self._lock:
            if req_id in self._pending_approvals:
                self._pending_approvals[req_id]["status"] = "approved" if approved else "denied"
                self._events[req_id].set()
                return True
            return False

    def get_pending(self) -> list[dict]:
        with self._lock:
            res = [{"id": rid, **req} for rid, req in self._pending_approvals.items()]
        return sorted(res, key=lambda x: x["timestamp"])


class ExecutionTracer:
    MAX_TRACES = 200

    def __init__(self):
        self._lock = threading.RLock()
        self._traces: dict[str, AgentTrace] = {}
        self.hitl = HITLManager(tracer=self)

    def start_trace(self, session_id: str, task: str) -> AgentTrace:
        tid = str(uuid.uuid4())[:8]
        trace = AgentTrace(id=tid, session_id=session_id, task=task)
        with self._lock:
            if len(self._traces) >= self.MAX_TRACES:
                completed = sorted(
                    (t for t in self._traces.values() if t.status in ("completed", "failed")),
                    key=lambda x: x.start_time,
                )
                for old in completed[: max(1, len(self._traces) - self.MAX_TRACES + 1)]:
                    self._traces.pop(old.id, None)
            self._traces[tid] = trace
        return trace

    def add_step(
        self,
        trace_id: str,
        step_type: str,
        content: str,
        tool_name: Optional[str] = None,
        tool_args: Optional[dict] = None,
        duration_ms: float = 0.0,
    ) -> TraceStep:
        now = time.time()
        with self._lock:
            if trace_id not in self._traces:
                raise KeyError(f"Trace {trace_id} not found")
            tr = self._traces[trace_id]
            if duration_ms <= 0.0:
                prev_ts = tr.steps[-1].timestamp if tr.steps else tr.start_time
                duration_ms = round(max(0.0, (now - prev_ts) * 1000.0), 2)
            step = TraceStep(
                id=str(uuid.uuid4())[:8],
                trace_id=trace_id,
                type=step_type,
                content=content,
                tool_name=tool_name,
                tool_args=tool_args,
                timestamp=now,
                duration_ms=duration_ms,
            )
            tr.steps.append(step)
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


_TRACER_INSTANCE: Optional[ExecutionTracer] = None


def get_tracer() -> ExecutionTracer:
    global _TRACER_INSTANCE
    if _TRACER_INSTANCE is None:
        _TRACER_INSTANCE = ExecutionTracer()
    return _TRACER_INSTANCE
