from __future__ import annotations

import abc
import asyncio
import heapq
import logging
import operator
import random
import sys
import warnings
import weakref
from collections import defaultdict, deque
from collections.abc import (
    Callable,
    Collection,
    Container,
    Iterator,
    Mapping,
    MutableMapping,
)
from copy import copy
from dataclasses import dataclass, field
from functools import lru_cache, partial, singledispatchmethod
from itertools import chain
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NamedTuple, TypedDict, cast

from tlz import peekn

import dask
from dask.utils import parse_bytes, typename

from distributed._stories import worker_story
from distributed.collections import HeapSet
from distributed.comm import get_address_host
from distributed.core import ErrorMessage, error_message
from distributed.metrics import time
from distributed.protocol import pickle
from distributed.protocol.serialize import Serialize
from distributed.sizeof import safe_sizeof as sizeof
from distributed.utils import recursive_to_dict

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # TODO import from typing (requires Python >=3.10)
    from typing_extensions import TypeAlias

    # Circular imports
    from distributed.diagnostics.plugin import WorkerPlugin
    from distributed.worker import Worker

    # TODO move out of TYPE_CHECKING (requires Python >=3.10)
    # Not to be confused with distributed.scheduler.TaskStateState
    TaskStateState: TypeAlias = Literal[
        "cancelled",
        "constrained",
        "error",
        "executing",
        "fetch",
        "flight",
        "forgotten",
        "long-running",
        "memory",
        "missing",
        "ready",
        "released",
        "rescheduled",
        "resumed",
        "waiting",
    ]
else:
    TaskStateState = str

# TaskState.state subsets
PROCESSING: set[TaskStateState] = {
    "waiting",
    "ready",
    "constrained",
    "executing",
    "long-running",
    "cancelled",
    "resumed",
}
READY: set[TaskStateState] = {"ready", "constrained"}
# Valid states for a task that is found in TaskState.waiting_for_data
WAITING_FOR_DATA: set[TaskStateState] = {
    "constrained",
    "executing",
    "fetch",
    "flight",
    "long-running",
    "missing",
    "ready",
    "resumed",
    "waiting",
}

NO_VALUE = "--no-value-sentinel--"


class SerializedTask(NamedTuple):
    """Info from distributed.scheduler.TaskState.run_spec
    Input to distributed.worker._deserialize

    (function, args kwargs) and task are mutually exclusive
    """

    function: bytes | None = None
    args: bytes | tuple | list | None = None
    kwargs: bytes | dict[str, Any] | None = None
    task: object = NO_VALUE


class StartStop(TypedDict, total=False):
    action: str
    start: float
    stop: float
    source: str  # optional


class InvalidTransition(Exception):
    def __init__(
        self,
        key: str,
        start: TaskStateState,
        finish: TaskStateState,
        story: list[tuple],
    ):
        self.key = key
        self.start = start
        self.finish = finish
        self.story = story

    def __reduce__(self) -> tuple[Callable, tuple]:
        return type(self), (self.key, self.start, self.finish, self.story)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}: {self.key} :: {self.start}->{self.finish}"
            + "\n"
            + "  Story:\n    "
            + "\n    ".join(map(str, self.story))
        )

    __str__ = __repr__

    def to_event(self) -> tuple[str, dict[str, Any]]:
        return (
            "invalid-worker-transition",
            {
                "key": self.key,
                "start": self.start,
                "finish": self.finish,
                "story": self.story,
            },
        )


class TransitionCounterMaxExceeded(InvalidTransition):
    def to_event(self) -> tuple[str, dict[str, Any]]:
        topic, msg = super().to_event()
        return "transition-counter-max-exceeded", msg


class InvalidTaskState(Exception):
    def __init__(
        self,
        key: str,
        state: TaskStateState,
        story: list[tuple],
    ):
        self.key = key
        self.state = state
        self.story = story

    def __reduce__(self) -> tuple[Callable, tuple]:
        return type(self), (self.key, self.state, self.story)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}: {self.key} :: {self.state}"
            + "\n"
            + "  Story:\n    "
            + "\n    ".join(map(str, self.story))
        )

    __str__ = __repr__

    def to_event(self) -> tuple[str, dict[str, Any]]:
        return (
            "invalid-worker-task-state",
            {
                "key": self.key,
                "state": self.state,
                "story": self.story,
            },
        )


class RecommendationsConflict(Exception):
    """Two or more recommendations for the same task suggested different finish states"""


@lru_cache
def _default_data_size() -> int:
    return parse_bytes(dask.config.get("distributed.scheduler.default-data-size"))


# Note: can't specify __slots__ manually to enable slots in Python <3.10 in a @dataclass
# that defines any default values
dc_slots = {"slots": True} if sys.version_info >= (3, 10) else {}


@dataclass(repr=False, eq=False, **dc_slots)
class TaskState:
    """Holds volatile state relating to an individual Dask task.

    Not to be confused with :class:`distributed.scheduler.TaskState`, which holds
    similar information on the scheduler side.
    """

    #: Task key. Mandatory.
    key: str
    #: A named tuple containing the ``function``, ``args``, ``kwargs`` and ``task``
    #: associated with this `TaskState` instance. This defaults to ``None`` and can
    #: remain empty if it is a dependency that this worker will receive from another
    #: worker.
    run_spec: SerializedTask | None = None

    #: The data needed by this key to run
    dependencies: set[TaskState] = field(default_factory=set)
    #: The keys that use this dependency
    dependents: set[TaskState] = field(default_factory=set)
    #: Subset of dependencies that are not in memory
    waiting_for_data: set[TaskState] = field(default_factory=set)
    #: Subset of dependents that are not in memory
    waiters: set[TaskState] = field(default_factory=set)

    #: The current state of the task
    state: TaskStateState = "released"
    #: The previous state of the task. It is not None iff :attr:`state` in
    #: (cancelled, resumed).
    previous: Literal["executing", "long-running", "flight", None] = None
    #: The next state of the task. It is not None iff :attr:`state` == resumed.
    next: Literal["fetch", "waiting", None] = None

    #: Expected duration of the task
    duration: float | None = None
    #: The priority this task given by the scheduler. Determines run order.
    priority: tuple[int, ...] | None = None
    #: Addresses of workers that we believe have this data
    who_has: set[str] = field(default_factory=set)
    #: The worker that current task data is coming from if task is in flight
    coming_from: str | None = None
    #: Abstract resources required to run a task
    resource_restrictions: dict[str, float] = field(default_factory=dict)
    #: The exception caused by running a task if it erred (serialized)
    exception: Serialize | None = None
    #: The traceback caused by running a task if it erred (serialized)
    traceback: Serialize | None = None
    #: string representation of exception
    exception_text: str = ""
    #: string representation of traceback
    traceback_text: str = ""
    #: The type of a particular piece of data
    type: type | None = None
    #: The number of times a dependency has not been where we expected it
    suspicious_count: int = 0
    #: Log of transfer, load, and compute times for a task
    startstops: list[StartStop] = field(default_factory=list)
    #: Time at which task begins running
    start_time: float | None = None
    #: Time at which task finishes running
    stop_time: float | None = None
    #: Metadata related to the task.
    #: Stored metadata should be msgpack serializable (e.g. int, string, list, dict).
    metadata: dict = field(default_factory=dict)
    #: The size of the value of the task, if in memory
    nbytes: int | None = None
    #: Arbitrary task annotations
    annotations: dict | None = None
    #: True if the :meth:`~WorkerBase.execute` or :meth:`~WorkerBase.gather_dep`
    #: coroutine servicing this task completed; False otherwise. This flag changes
    #: the behaviour of transitions out of the ``executing``, ``flight`` etc. states.
    done: bool = False

    _instances: ClassVar[weakref.WeakSet[TaskState]] = weakref.WeakSet()

    # Support for weakrefs to a class with __slots__
    __weakref__: Any = field(init=False)

    def __post_init__(self) -> None:
        TaskState._instances.add(self)

    def __repr__(self) -> str:
        if self.state == "cancelled":
            state = f"cancelled({self.previous})"
        elif self.state == "resumed":
            state = f"resumed({self.previous}->{self.next})"
        else:
            state = self.state
        return f"<TaskState {self.key!r} {state}>"

    def __hash__(self) -> int:
        """Override dataclass __hash__, reverting to the default behaviour
        hash(o) == id(o).

        Note that we also defined @dataclass(eq=False), which reverts to the default
        behaviour (a == b) == (a is b).

        On first thought, it would make sense to use TaskState.key for equality and
        hashing. However, a task may be forgotten and a new TaskState object with the
        same key may be created in its place later on. In the Worker state, you should
        never have multiple TaskState objects with the same key; see
        WorkerState.validate_state for relevant checks. We can't assert the same thing
        in __eq__ though, as multiple objects with the same key may appear in
        TaskState._instances for a brief period of time.
        """
        return id(self)

    def get_nbytes(self) -> int:
        nbytes = self.nbytes
        return nbytes if nbytes is not None else _default_data_size()

    def _to_dict_no_nest(self, *, exclude: Container[str] = ()) -> dict:
        """Dictionary representation for debugging purposes.
        Not type stable and not intended for roundtrips.

        See also
        --------
        Client.dump_cluster_state
        distributed.utils.recursive_to_dict

        Notes
        -----
        This class uses ``_to_dict_no_nest`` instead of ``_to_dict``.
        When a task references another task, just print the task repr. All tasks
        should neatly appear under Worker.tasks. This also prevents a RecursionError
        during particularly heavy loads, which have been observed to happen whenever
        there's an acyclic dependency chain of ~200+ tasks.
        """
        out = recursive_to_dict(self, exclude=exclude, members=True)
        # Remove all Nones and empty containers
        return {k: v for k, v in out.items() if v}

    def is_protected(self) -> bool:
        return self.state in PROCESSING or any(
            dep_ts.state in PROCESSING for dep_ts in self.dependents
        )


@dataclass
class Instruction:
    """Command from the worker state machine to the Worker, in response to an event"""

    __slots__ = ("stimulus_id",)
    stimulus_id: str

    @classmethod
    def match(cls, **kwargs: Any) -> _InstructionMatch:
        """Generate a partial match to compare against an Instruction instance.
        The typical usage is to compare a list of instructions returned by
        :meth:`WorkerState.handle_stimulus` or in :attr:`WorkerState.stimulus_log` vs.
        an expected list of matches.

        Examples
        --------

        .. code-block:: python

            instructions = ws.handle_stimulus(...)
            assert instructions == [
                TaskFinishedMsg.match(key="x"),
                ...
            ]
        """
        return _InstructionMatch(cls, **kwargs)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _InstructionMatch):
            return other == self
        else:
            # Revert to default dataclass behaviour
            return super().__eq__(other)


class _InstructionMatch:
    """Utility class, to be used to test an instructions list.
    See :meth:`Instruction.match`.
    """

    cls: type[Instruction]
    kwargs: dict[str, Any]

    def __init__(self, cls: type[Instruction], **kwargs: Any):
        self.cls = cls
        self.kwargs = kwargs

    def __repr__(self) -> str:
        cls_str = self.cls.__name__
        kwargs_str = ", ".join(f"{k}={v}" for k, v in self.kwargs.items())
        return f"{cls_str}({kwargs_str}) (partial match)"

    def __eq__(self, other: object) -> bool:
        if type(other) is not self.cls:
            return False
        return all(getattr(other, k) == v for k, v in self.kwargs.items())


@dataclass
class GatherDep(Instruction):
    __slots__ = ("worker", "to_gather", "total_nbytes")
    worker: str
    to_gather: set[str]
    total_nbytes: int


@dataclass
class Execute(Instruction):
    __slots__ = ("key",)
    key: str


@dataclass
class RetryBusyWorkerLater(Instruction):
    __slots__ = ("worker",)
    worker: str


class SendMessageToScheduler(Instruction):
    #: Matches a key in Scheduler.stream_handlers
    op: ClassVar[str]
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        """Convert object to dict so that it can be serialized with msgpack"""
        d = {k: getattr(self, k) for k in self.__annotations__}
        d["op"] = self.op
        d["stimulus_id"] = self.stimulus_id
        return d


@dataclass
class TaskFinishedMsg(SendMessageToScheduler):
    op = "task-finished"

    key: str
    nbytes: int | None
    type: bytes  # serialized class
    typename: str
    metadata: dict
    thread: int | None
    startstops: list[StartStop]
    __slots__ = tuple(__annotations__)

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["status"] = "OK"
        return d


@dataclass
class TaskErredMsg(SendMessageToScheduler):
    op = "task-erred"

    key: str
    exception: Serialize
    traceback: Serialize | None
    exception_text: str
    traceback_text: str
    thread: int | None
    startstops: list[StartStop]
    __slots__ = tuple(__annotations__)

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["status"] = "error"
        return d

    @staticmethod
    def from_task(
        ts: TaskState, stimulus_id: str, thread: int | None = None
    ) -> TaskErredMsg:
        assert ts.exception
        return TaskErredMsg(
            key=ts.key,
            exception=ts.exception,
            traceback=ts.traceback,
            exception_text=ts.exception_text,
            traceback_text=ts.traceback_text,
            thread=thread,
            startstops=ts.startstops,
            stimulus_id=stimulus_id,
        )


@dataclass
class ReleaseWorkerDataMsg(SendMessageToScheduler):
    op = "release-worker-data"

    __slots__ = ("key",)
    key: str


# Not to be confused with RescheduleEvent below or the distributed.Reschedule Exception
@dataclass
class RescheduleMsg(SendMessageToScheduler):
    op = "reschedule"

    __slots__ = ("key",)
    key: str


@dataclass
class LongRunningMsg(SendMessageToScheduler):
    op = "long-running"

    __slots__ = ("key", "compute_duration")
    key: str
    compute_duration: float | None


@dataclass
class AddKeysMsg(SendMessageToScheduler):
    op = "add-keys"

    __slots__ = ("keys",)
    keys: Collection[str]


@dataclass
class RequestRefreshWhoHasMsg(SendMessageToScheduler):
    """Worker -> Scheduler asynchronous request for updated who_has information.
    Not to be confused with the scheduler.who_has synchronous RPC call, which is used
    by the Client.

    See also
    --------
    RefreshWhoHasEvent
    distributed.scheduler.Scheduler.request_refresh_who_has
    distributed.client.Client.who_has
    distributed.scheduler.Scheduler.get_who_has
    """

    op = "request-refresh-who-has"

    __slots__ = ("keys",)
    keys: Collection[str]


@dataclass
class StealResponseMsg(SendMessageToScheduler):
    """Worker->Scheduler response to ``{op: steal-request}``

    See also
    --------
    StealRequestEvent
    """

    op = "steal-response"

    __slots__ = ("key", "state")
    key: str
    state: TaskStateState | None


@dataclass
class StateMachineEvent:
    """Base abstract class for all stimuli that can modify the worker state"""

    __slots__ = ("stimulus_id", "handled")
    #: Unique ID of the event
    stimulus_id: str
    #: timestamp of when the event was handled by the worker
    # TODO Switch to @dataclass(slots=True), uncomment the line below, and remove the
    #      __new__ method (requires Python >=3.10)
    # handled: float | None = field(init=False, default=None)
    _classes: ClassVar[dict[str, type[StateMachineEvent]]] = {}

    def __new__(cls, *args: Any, **kwargs: Any) -> StateMachineEvent:
        """Hack to initialize the ``handled`` attribute in Python <3.10"""
        self = object.__new__(cls)
        self.handled = None
        return self

    def __init_subclass__(cls) -> None:
        StateMachineEvent._classes[cls.__name__] = cls

    def to_loggable(self, *, handled: float) -> StateMachineEvent:
        """Produce a variant version of self that is small enough to be stored in memory
        in the medium term and contains meaningful information for debugging
        """
        self.handled: float | None = handled
        return self

    def _to_dict(self, *, exclude: Container[str] = ()) -> dict:
        """Dictionary representation for debugging purposes.

        See also
        --------
        distributed.utils.recursive_to_dict
        """
        info = {"cls": type(self).__name__}
        for k in dir(self):
            if k in exclude or k.startswith("_"):
                continue
            v = getattr(self, k)
            if not callable(v):
                info[k] = v
        return recursive_to_dict(info, exclude=exclude)

    @staticmethod
    def from_dict(d: dict) -> StateMachineEvent:
        """Convert the output of ``recursive_to_dict`` back into the original object.
        The output object is meaningful for the purpose of rebuilding the state machine,
        but not necessarily identical to the original.
        """
        kwargs = d.copy()
        cls = StateMachineEvent._classes[kwargs.pop("cls")]
        handled = kwargs.pop("handled")
        inst = cls(**kwargs)
        inst.handled = handled
        inst._after_from_dict()
        return inst

    def _after_from_dict(self) -> None:
        """Optional post-processing after an instance is created by ``from_dict``"""


@dataclass
class PauseEvent(StateMachineEvent):
    __slots__ = ()


@dataclass
class UnpauseEvent(StateMachineEvent):
    __slots__ = ()


@dataclass
class RetryBusyWorkerEvent(StateMachineEvent):
    __slots__ = ("worker",)
    worker: str


@dataclass
class GatherDepDoneEvent(StateMachineEvent):
    """:class:`GatherDep` instruction terminated (abstract base class)"""

    __slots__ = ("worker", "total_nbytes")
    worker: str
    total_nbytes: int  # Must be the same as in GatherDep instruction


@dataclass
class GatherDepSuccessEvent(GatherDepDoneEvent):
    """:class:`GatherDep` instruction terminated:
    remote worker fetched successfully
    """

    __slots__ = ("data",)

    data: dict[str, object]  # There may be less keys than in GatherDep

    def to_loggable(self, *, handled: float) -> StateMachineEvent:
        out = copy(self)
        out.handled = handled
        out.data = {k: None for k in self.data}
        return out

    def _after_from_dict(self) -> None:
        self.data = {k: None for k in self.data}


@dataclass
class GatherDepBusyEvent(GatherDepDoneEvent):
    """:class:`GatherDep` instruction terminated:
    remote worker is busy
    """

    __slots__ = ()


@dataclass
class GatherDepNetworkFailureEvent(GatherDepDoneEvent):
    """:class:`GatherDep` instruction terminated:
    network failure while trying to communicate with remote worker
    """

    __slots__ = ()


@dataclass
class GatherDepFailureEvent(GatherDepDoneEvent):
    """class:`GatherDep` instruction terminated:
    generic error raised (not a network failure); e.g. data failed to deserialize.
    """

    exception: Serialize
    traceback: Serialize | None
    exception_text: str
    traceback_text: str
    __slots__ = tuple(__annotations__)

    def _after_from_dict(self) -> None:
        self.exception = Serialize(Exception())
        self.traceback = None

    @classmethod
    def from_exception(
        cls,
        err: BaseException,
        *,
        worker: str,
        total_nbytes: int,
        stimulus_id: str,
    ) -> GatherDepFailureEvent:
        msg = error_message(err)
        return cls(
            worker=worker,
            total_nbytes=total_nbytes,
            exception=msg["exception"],
            traceback=msg["traceback"],
            exception_text=msg["exception_text"],
            traceback_text=msg["traceback_text"],
            stimulus_id=stimulus_id,
        )


@dataclass
class ComputeTaskEvent(StateMachineEvent):
    key: str
    who_has: dict[str, Collection[str]]
    nbytes: dict[str, int]
    priority: tuple[int, ...]
    duration: float
    run_spec: SerializedTask | None
    function: bytes | None
    args: bytes | tuple | list | None | None
    kwargs: bytes | dict[str, Any] | None
    resource_restrictions: dict[str, float]
    actor: bool
    annotations: dict
    __slots__ = tuple(__annotations__)

    def __post_init__(self) -> None:
        # Fixes after msgpack decode
        if isinstance(self.priority, list):  # type: ignore[unreachable]
            self.priority = tuple(self.priority)  # type: ignore[unreachable]

        if self.function is not None:
            assert self.run_spec is None
            self.run_spec = SerializedTask(
                function=self.function, args=self.args, kwargs=self.kwargs
            )
        elif not isinstance(self.run_spec, SerializedTask):
            self.run_spec = SerializedTask(task=self.run_spec)

    def _to_dict(self, *, exclude: Container[str] = ()) -> dict:
        return StateMachineEvent._to_dict(self._clean(), exclude=exclude)

    def _clean(self) -> StateMachineEvent:
        out = copy(self)
        out.function = None
        out.kwargs = None
        out.args = None
        out.run_spec = SerializedTask(task=None, function=None, args=None, kwargs=None)
        return out

    def to_loggable(self, *, handled: float) -> StateMachineEvent:
        out = self._clean()
        out.handled = handled
        return out

    def _after_from_dict(self) -> None:
        self.run_spec = SerializedTask(task=None, function=None, args=None, kwargs=None)

    @staticmethod
    def dummy(
        key: str,
        *,
        who_has: dict[str, Collection[str]] | None = None,
        nbytes: dict[str, int] | None = None,
        priority: tuple[int, ...] = (0,),
        duration: float = 1.0,
        resource_restrictions: dict[str, float] | None = None,
        actor: bool = False,
        annotations: dict | None = None,
        stimulus_id: str,
    ) -> ComputeTaskEvent:
        """Build a dummy event, with most attributes set to a reasonable default.
        This is a convenience method to be used in unit testing only.
        """
        return ComputeTaskEvent(
            key=key,
            who_has=who_has or {},
            nbytes=nbytes or {k: 1 for k in who_has or ()},
            priority=priority,
            duration=duration,
            run_spec=None,
            function=None,
            args=None,
            kwargs=None,
            resource_restrictions=resource_restrictions or {},
            actor=actor,
            annotations=annotations or {},
            stimulus_id=stimulus_id,
        )


@dataclass
class ExecuteDoneEvent(StateMachineEvent):
    """Abstract base event for all the possible outcomes of a :class:`Compute`
    instruction
    """

    key: str
    __slots__ = ("key",)


@dataclass
class ExecuteSuccessEvent(ExecuteDoneEvent):
    value: object
    start: float
    stop: float
    nbytes: int
    type: type | None
    __slots__ = tuple(__annotations__)

    def to_loggable(self, *, handled: float) -> StateMachineEvent:
        out = copy(self)
        out.handled = handled
        out.value = None
        return out

    def _to_dict(self, *, exclude: Container[str] = ()) -> dict:
        d = super()._to_dict(exclude=exclude)
        # This is excluded by the parent class as it is a callable
        if "type" not in exclude:
            d["type"] = str(self.type)
        return d

    def _after_from_dict(self) -> None:
        self.value = None
        self.type = None

    @staticmethod
    def dummy(
        key: str,
        value: object = None,
        *,
        nbytes: int = 1,
        stimulus_id: str,
    ) -> ExecuteSuccessEvent:
        """Build a dummy event, with most attributes set to a reasonable default.
        This is a convenience method to be used in unit testing only.
        """
        return ExecuteSuccessEvent(
            key=key,
            value=value,
            start=0.0,
            stop=1.0,
            nbytes=nbytes,
            type=None,
            stimulus_id=stimulus_id,
        )


@dataclass
class ExecuteFailureEvent(ExecuteDoneEvent):
    start: float | None
    stop: float | None
    exception: Serialize
    traceback: Serialize | None
    exception_text: str
    traceback_text: str
    __slots__ = tuple(__annotations__)

    def _after_from_dict(self) -> None:
        self.exception = Serialize(Exception())
        self.traceback = None

    @classmethod
    def from_exception(
        cls,
        err_or_msg: BaseException | ErrorMessage,
        *,
        key: str,
        start: float | None = None,
        stop: float | None = None,
        stimulus_id: str,
    ) -> ExecuteFailureEvent:
        if isinstance(err_or_msg, dict):
            msg = err_or_msg
        else:
            msg = error_message(err_or_msg)

        return cls(
            key=key,
            start=start,
            stop=stop,
            exception=msg["exception"],
            traceback=msg["traceback"],
            exception_text=msg["exception_text"],
            traceback_text=msg["traceback_text"],
            stimulus_id=stimulus_id,
        )

    @staticmethod
    def dummy(
        key: str,
        *,
        stimulus_id: str,
    ) -> ExecuteFailureEvent:
        """Build a dummy event, with most attributes set to a reasonable default.
        This is a convenience method to be used in unit testing only.
        """
        return ExecuteFailureEvent(
            key=key,
            start=None,
            stop=None,
            exception=Serialize(None),
            traceback=None,
            exception_text="",
            traceback_text="",
            stimulus_id=stimulus_id,
        )


# Not to be confused with RescheduleMsg above or the distributed.Reschedule Exception
@dataclass
class RescheduleEvent(ExecuteDoneEvent):
    __slots__ = ()

    @staticmethod
    def dummy(key: str, *, stimulus_id: str) -> RescheduleEvent:
        """Build an event. This method exists for compatibility with the other
        ExecuteDoneEvent subclasses.
        """
        return RescheduleEvent(key=key, stimulus_id=stimulus_id)


@dataclass
class CancelComputeEvent(StateMachineEvent):
    __slots__ = ("key",)
    key: str


@dataclass
class FindMissingEvent(StateMachineEvent):
    __slots__ = ()


@dataclass
class RefreshWhoHasEvent(StateMachineEvent):
    """Scheduler -> Worker message containing updated who_has information.

    See also
    --------
    RequestRefreshWhoHasMsg
    """

    __slots__ = ("who_has",)
    # {key: [worker address, ...]}
    who_has: dict[str, Collection[str]]


@dataclass
class AcquireReplicasEvent(StateMachineEvent):
    __slots__ = ("who_has", "nbytes")
    who_has: dict[str, Collection[str]]
    nbytes: dict[str, int]


@dataclass
class RemoveReplicasEvent(StateMachineEvent):
    __slots__ = ("keys",)
    keys: Collection[str]


@dataclass
class FreeKeysEvent(StateMachineEvent):
    __slots__ = ("keys",)
    keys: Collection[str]


@dataclass
class StealRequestEvent(StateMachineEvent):
    """Event that requests a worker to release a key because it's now being computed
    somewhere else.

    See also
    --------
    StealResponseMsg
    """

    __slots__ = ("key",)
    key: str


@dataclass
class UpdateDataEvent(StateMachineEvent):
    __slots__ = ("data", "report")
    data: dict[str, object]
    report: bool

    def to_loggable(self, *, handled: float) -> StateMachineEvent:
        out = copy(self)
        out.handled = handled
        out.data = dict.fromkeys(self.data)
        return out


@dataclass
class SecedeEvent(StateMachineEvent):
    __slots__ = ("key", "compute_duration")
    key: str
    compute_duration: float


if TYPE_CHECKING:
    # TODO remove quotes (requires Python >=3.9)
    # TODO get out of TYPE_CHECKING (requires Python >=3.10)
    # {TaskState -> finish: TaskStateState | (finish: TaskStateState, transition *args)}
    # Not to be confused with distributed.scheduler.Recs
    Recs: TypeAlias = "dict[TaskState, TaskStateState | tuple]"
    Instructions: TypeAlias = "list[Instruction]"
    RecsInstrs: TypeAlias = "tuple[Recs, Instructions]"
else:
    Recs = dict
    Instructions = list
    RecsInstrs = tuple


def merge_recs_instructions(*args: RecsInstrs) -> RecsInstrs:
    """Merge multiple (recommendations, instructions) tuples.
    Collisions in recommendations are only allowed if identical.
    """
    recs: Recs = {}
    instr: Instructions = []
    for recs_i, instr_i in args:
        for ts, finish in recs_i.items():
            if ts in recs and recs[ts] != finish:
                raise RecommendationsConflict(
                    f"Mismatched recommendations for {ts.key}: {recs[ts]} vs. {finish}"
                )
            recs[ts] = finish
        instr += instr_i
    return recs, instr


class WorkerState:
    """State machine encapsulating the lifetime of all tasks on a worker.

    Not to be confused with :class:`distributed.scheduler.WorkerState`.

    .. note::
       The data attributes of this class are implementation details and may be
       changed without a deprecation cycle.

    .. warning::
       The attributes of this class are all heavily correlated with each other.
       *Do not* modify them directly, *ever*, as it is extremely easy to obtain a broken
       state this way, which in turn will likely result in cluster-wide deadlocks.

       The state should be exclusively mutated through :meth:`handle_stimulus`.
    """

    #: Worker <IP address>:<port>. This is used in decision-making by the state machine,
    #: e.g. to determine if a peer worker is running on the same host or not.
    #: This attribute may not be known when the WorkerState is initialised. It *must* be
    #: set before the first call to :meth:`handle_stimulus`.
    address: str

    #: ``{key: TaskState}``. The tasks currently executing on this worker (and any
    #: dependencies of those tasks)
    tasks: dict[str, TaskState]

    #: ``{ts.key: thread ID}``. This collection is shared by reference between
    #: :class:`~distributed.worker.Worker` and this class. While the WorkerState is
    #: thread-agnostic, it still needs access to this information in some cases.
    #: This collection is populated by :meth:`distributed.worker.Worker.execute`.
    #: It does not *need* to be populated for the WorkerState to work.
    threads: dict[str, int]

    #: In-memory tasks data. This collection is shared by reference between
    #: :class:`~distributed.worker.Worker`,
    #: :class:`~distributed.worker_memory.WorkerMemoryManager`, and this class.
    data: MutableMapping[str, object]

    #: ``{name: worker plugin}``. This collection is shared by reference between
    #: :class:`~distributed.worker.Worker` and this class. The Worker managed adding and
    #: removing plugins, while the WorkerState invokes the ``WorkerPlugin.transition``
    #: method, is available.
    plugins: dict[str, WorkerPlugin]

    #: Priority heap of tasks that are ready to run and have no resource constrains.
    #: Mutually exclusive with :attr:`constrained`.
    ready: HeapSet[TaskState]

    #: Priority heap of tasks that are ready to run, but are waiting on abstract
    #: resources like GPUs. Mutually exclusive with :attr:`ready`.
    #: See :attr:`available_resources` and :doc:`resources`.
    constrained: HeapSet[TaskState]

    #: Number of tasks that can be executing in parallel.
    #: At any given time, :meth:`executing_count` <= nthreads.
    nthreads: int

    #: True if the state machine should start executing more tasks and fetch
    #: dependencies whenever a slot is available. This property must be kept aligned
    #: with the Worker: ``WorkerState.running == (Worker.status is Status.running)``.
    running: bool

    #: A count of how many tasks are currently waiting for data
    waiting_for_data_count: int

    #: ``{worker address: {ts.key, ...}``.
    #: The data that we care about that we think a worker has
    has_what: defaultdict[str, set[str]]

    #: The tasks which still require data in order to execute and are in memory on at
    #: least another worker, prioritized as per-worker heaps. All and only tasks with
    #: ``TaskState.state == 'fetch'`` are in this collection. A :class:`TaskState` with
    #: multiple entries in :attr:`~TaskState.who_has` will appear multiple times here.
    data_needed: defaultdict[str, HeapSet[TaskState]]

    #: Number of bytes to fetch from the same worker in a single call to
    #: :meth:`BaseWorker.gather_dep`. Multiple small tasks that can be fetched from the
    #: same worker will be clustered in a single instruction as long as their combined
    #: size doesn't exceed this value.
    transfer_message_target_bytes: int

    #: All and only tasks with ``TaskState.state == 'missing'``.
    missing_dep_flight: set[TaskState]

    #: Tasks that are coming to us in current peer-to-peer connections.
    #:
    #: This set includes exclusively tasks with :attr:`~TaskState.state` == 'flight' as
    #: well as tasks with :attr:`~TaskState.state` in ('cancelled', 'resumed') and
    #: :attr:`~TaskState.previous` == 'flight`.
    #:
    #: See also :meth:`in_flight_tasks_count`.
    in_flight_tasks: set[TaskState]

    #: ``{worker address: {ts.key, ...}}``
    #: The workers from which we are currently gathering data and the dependencies we
    #: expect from those connections. Workers in this dict won't be asked for additional
    #: dependencies until the current query returns.
    in_flight_workers: dict[str, set[str]]

    #: Current total size of open data transfers from other workers
    transfer_incoming_bytes: int

    #: Maximum number of concurrent incoming data transfers from other workers.
    #: See also :attr:`distributed.worker.Worker.transfer_outgoing_count_limit`.
    transfer_incoming_count_limit: int

    #: Total number of data transfers from other workers since the worker was started.
    transfer_incoming_count_total: int

    #: Ignore :attr:`transfer_incoming_count_limit` as long as :attr:`transfer_incoming_bytes` is
    #: less than this value.
    transfer_incoming_bytes_throttle_threshold: int

    #: Peer workers that recently returned a busy status. Workers in this set won't be
    #: asked for additional dependencies for some time.
    busy_workers: set[str]

    #: Counter that decreases every time the compute-task handler is invoked by the
    #: Scheduler. It is appended to :attr:`TaskState.priority` and acts as a
    #: tie-breaker between tasks that have the same priority on the Scheduler,
    #: determining a last-in-first-out order between them.
    generation: int

    #: ``{resource name: amount}``. Total resources available for task execution.
    #: See :doc: `resources`.
    total_resources: dict[str, float]

    #: ``{resource name: amount}``. Current resources that aren't being currently
    #: consumed by task execution. Always less or equal to :attr:`total_resources`.
    #: See :doc:`resources`.
    available_resources: dict[str, float]

    #: Set of tasks that are currently running.
    #:
    #: This set includes exclusively tasks with :attr:`~TaskState.state` == 'executing'
    #: as well as tasks with :attr:`~TaskState.state` in ('cancelled', 'resumed') and
    #: :attr:`~TaskState.previous` == 'executing`.
    #:
    #: See also :meth:`executing_count` and :attr:`long_running`.
    executing: set[TaskState]

    #: Set of tasks that are currently running and have called
    #: :func:`~distributed.secede`, so they no longer count towards the maximum number
    #: of concurrent tasks (nthreads).
    #: These tasks do not appear in the :attr:`executing` set.
    #:
    #: This set includes exclusively tasks with
    #: :attr:`~TaskState.state` == 'long-running' as well as tasks with
    #: :attr:`~TaskState.state` in ('cancelled', 'resumed') and
    #: :attr:`~TaskState.previous` == 'long-running`.
    long_running: set[TaskState]

    #: A number of tasks that this worker has run in its lifetime; this includes failed
    #: and cancelled tasks. See also :meth:`executing_count`.
    executed_count: int

    #: Actor tasks. See :doc:`actors`.
    actors: dict[str, object]

    #: Transition log: ``[(..., stimulus_id: str | None, timestamp: float), ...]``
    #: The number of stimuli logged is capped.
    #: See also :meth:`story` and :attr:`stimulus_log`.
    log: deque[tuple]

    #: Log of all stimuli received by :meth:`handle_stimulus`.
    #: The number of events logged is capped.
    #: See also :attr:`log` and :meth:`stimulus_story`.
    stimulus_log: deque[StateMachineEvent]

    #: If True, enable expensive internal consistency check.
    #: Typically disabled in production.
    validate: bool

    #: Total number of state transitions so far.
    #: See also :attr:`log` and :attr:`transition_counter_max`.
    transition_counter: int

    #: Raise an error if the :attr:`transition_counter` ever reaches this value.
    #: This is meant for debugging only, to catch infinite recursion loops.
    #: In production, it should always be set to False.
    transition_counter_max: int | Literal[False]

    #: Limit of bytes for incoming data transfers; this is used for throttling.
    transfer_incoming_bytes_limit: int | None

    #: Statically-seeded random state, used to guarantee determinism whenever a
    #: pseudo-random choice is required
    rng: random.Random

    __slots__ = tuple(__annotations__)

    def __init__(
        self,
        *,
        nthreads: int = 1,
        address: str | None = None,
        data: MutableMapping[str, object] | None = None,
        threads: dict[str, int] | None = None,
        plugins: dict[str, WorkerPlugin] | None = None,
        resources: Mapping[str, float] | None = None,
        transfer_incoming_count_limit: int = 9999,
        validate: bool = True,
        transition_counter_max: int | Literal[False] = False,
        transfer_incoming_bytes_limit: int | None = None,
    ):
        self.nthreads = nthreads

        # address may not be known yet when the State Machine is initialised.
        # Raise AttributeError if a method tries reading it before it's been set.
        if address:
            self.address = address

        # These collections are normally passed by reference by the Worker.
        # For the sake of convenience, create independent ones during unit tests.
        self.data = data if data is not None else {}
        self.threads = threads if threads is not None else {}
        self.plugins = plugins if plugins is not None else {}
        self.total_resources = dict(resources) if resources is not None else {}
        self.available_resources = self.total_resources.copy()

        self.validate = validate
        self.tasks = {}
        self.running = True
        self.waiting_for_data_count = 0
        self.has_what = defaultdict(set)
        self.data_needed = defaultdict(
            partial(HeapSet[TaskState], key=operator.attrgetter("priority"))
        )
        self.in_flight_workers = {}
        self.busy_workers = set()
        self.transfer_incoming_count_limit = transfer_incoming_count_limit
        self.transfer_incoming_count_total = 0
        self.transfer_incoming_bytes_throttle_threshold = int(10e6)
        self.transfer_incoming_bytes = 0
        self.missing_dep_flight = set()
        self.generation = 0
        self.ready = HeapSet(key=operator.attrgetter("priority"))
        self.constrained = HeapSet(key=operator.attrgetter("priority"))
        self.executing = set()
        self.in_flight_tasks = set()
        self.executed_count = 0
        self.long_running = set()
        self.transfer_message_target_bytes = int(50e6)  # 50 MB
        self.log = deque(maxlen=100_000)
        self.stimulus_log = deque(maxlen=10_000)
        self.transition_counter = 0
        self.transition_counter_max = transition_counter_max
        self.transfer_incoming_bytes_limit = transfer_incoming_bytes_limit
        self.actors = {}
        self.rng = random.Random(0)

    def handle_stimulus(self, *stims: StateMachineEvent) -> Instructions:
        """Process one or more external events, transition relevant tasks to new states,
        and return a list of instructions to be executed as a consequence.

        See also
        --------
        BaseWorker.handle_stimulus
        """
        instructions = []
        handled = time()
        for stim in stims:
            if not isinstance(stim, FindMissingEvent):
                self.stimulus_log.append(stim.to_loggable(handled=handled))
            recs, instr = self._handle_event(stim)
            instructions += instr
            instructions += self._transitions(recs, stimulus_id=stim.stimulus_id)
        return instructions

    #############
    # Accessors #
    #############

    @property
    def executing_count(self) -> int:
        """Count of tasks currently executing on this worker and counting towards the
        maximum number of threads.

        It includes cancelled tasks, but does not include long running (a.k.a. seceded)
        tasks.

        See also
        --------
        WorkerState.executing
        WorkerState.executed_count
        WorkerState.nthreads
        WorkerState.all_running_tasks
        """
        return len(self.executing)

    @property
    def all_running_tasks(self) -> set[TaskState]:
        """All tasks that are currently occupying a thread. They may or may not count
        towards the maximum number of threads.

        These are:

        - ts.status in (executing, long-running)
        - ts.status in (cancelled, resumed) and ts.previous in (executing, long-running)

        See also
        --------
        WorkerState.executing_count
        """
        # Note: cancelled and resumed tasks are still in either of these sets
        return self.executing | self.long_running

    @property
    def in_flight_tasks_count(self) -> int:
        """Number of tasks currently being replicated from other workers to this one.

        See also
        --------
        WorkerState.in_flight_tasks
        """
        return len(self.in_flight_tasks)

    @property
    def transfer_incoming_count(self) -> int:
        """Current number of open data transfers from other workers.

        See also
        --------
        WorkerState.in_flight_workers
        """
        return len(self.in_flight_workers)

    #########################
    # Shared helper methods #
    #########################

    def _ensure_task_exists(
        self, key: str, *, priority: tuple[int, ...], stimulus_id: str
    ) -> TaskState:
        try:
            ts = self.tasks[key]
            logger.debug("Data task %s already known (stimulus_id=%s)", ts, stimulus_id)
        except KeyError:
            self.tasks[key] = ts = TaskState(key)
        if not ts.priority:
            assert priority
            ts.priority = priority

        self.log.append((key, "ensure-task-exists", ts.state, stimulus_id, time()))
        return ts

    def _update_who_has(self, who_has: Mapping[str, Collection[str]]) -> None:
        for key, workers in who_has.items():
            ts = self.tasks.get(key)
            if not ts:
                # The worker sent a refresh-who-has request to the scheduler but, by the
                # time the answer comes back, some of the keys have been forgotten.
                continue
            workers = set(workers)

            if self.address in workers:
                workers.remove(self.address)
                # This can only happen if rebalance() recently asked to release a key,
                # but the RPC call hasn't returned yet. rebalance() is flagged as not
                # being safe to run while the cluster is not at rest and has already
                # been penned in to be redesigned on top of the AMM.
                # It is not necessary to send a message back to the
                # scheduler here, because it is guaranteed that there's already a
                # release-worker-data message in transit to it.
                if ts.state != "memory":
                    logger.debug(  # pragma: nocover
                        "Scheduler claims worker %s holds data for task %s, "
                        "which is not true.",
                        self.address,
                        ts,
                    )

            if ts.who_has == workers:
                continue

            for worker in ts.who_has - workers:
                self.has_what[worker].remove(key)
                if ts.state == "fetch":
                    self.data_needed[worker].remove(ts)

            for worker in workers - ts.who_has:
                self.has_what[worker].add(key)
                if ts.state == "fetch":
                    self.data_needed[worker].add(ts)

            ts.who_has = workers

    def _purge_state(self, ts: TaskState) -> None:
        """Ensure that TaskState attributes are reset to a neutral default and
        Worker-level state associated to the provided key is cleared (e.g.
        who_has)
        This is idempotent
        """
        logger.debug("Purge task: %s", ts)
        key = ts.key
        self.data.pop(key, None)
        self.actors.pop(key, None)

        for worker in ts.who_has:
            self.has_what[worker].discard(ts.key)
            self.data_needed[worker].discard(ts)
        ts.who_has.clear()

        self.threads.pop(key, None)

        for d in ts.dependencies:
            ts.waiting_for_data.discard(d)
            d.waiters.discard(ts)

        ts.waiting_for_data.clear()
        ts.nbytes = None
        ts.previous = None
        ts.next = None
        ts.done = False
        ts.coming_from = None

        self.missing_dep_flight.discard(ts)
        self.ready.discard(ts)
        self.constrained.discard(ts)
        self.executing.discard(ts)
        self.long_running.discard(ts)
        self.in_flight_tasks.discard(ts)

    def _should_throttle_incoming_transfers(self) -> bool:
        """Decides whether the WorkerState should throttle data transfers from other workers.

        Returns
        -------
        * True if the number of incoming data transfers reached its limit
        and the size of incoming data transfers reached the minimum threshold for throttling
        * True if the size of incoming data transfers reached its limit
        * False otherwise
        """
        reached_count_limit = (
            self.transfer_incoming_count >= self.transfer_incoming_count_limit
        )
        reached_throttle_threshold = (
            self.transfer_incoming_bytes
            >= self.transfer_incoming_bytes_throttle_threshold
        )
        reached_bytes_limit = (
            self.transfer_incoming_bytes_limit is not None
            and self.transfer_incoming_bytes >= self.transfer_incoming_bytes_limit
        )
        return reached_count_limit and reached_throttle_threshold or reached_bytes_limit

    def _ensure_communicating(self, *, stimulus_id: str) -> RecsInstrs:
        """Transition tasks from fetch to flight, until there are no more tasks in fetch
        state or a threshold has been reached.
        """
        if not self.running or not self.data_needed:
            return {}, []
        if self._should_throttle_incoming_transfers():
            return {}, []

        recommendations: Recs = {}
        instructions: Instructions = []

        for worker, available_tasks in self._select_workers_for_gather():
            assert worker != self.address
            to_gather_tasks, total_nbytes = self._select_keys_for_gather(
                available_tasks
            )
            # We always load at least one task
            assert to_gather_tasks or self.transfer_incoming_bytes
            # ...but that task might be selected in the previous iteration of the loop
            if not to_gather_tasks:
                break

            to_gather_keys = {ts.key for ts in to_gather_tasks}

            logger.debug(
                "Gathering %d tasks from %s; %d more remain. "
                "Pending workers: %d; connections: %d/%d; busy: %d",
                len(to_gather_tasks),
                worker,
                len(available_tasks),
                len(self.data_needed),
                self.transfer_incoming_count,
                self.transfer_incoming_count_limit,
                len(self.busy_workers),
            )
            self.log.append(
                ("gather-dependencies", worker, to_gather_keys, stimulus_id, time())
            )

            for ts in to_gather_tasks:
                if self.validate:
                    assert ts.state == "fetch"
                    assert worker in ts.who_has
                    assert ts not in recommendations
                recommendations[ts] = ("flight", worker)

            # A single invocation of _ensure_communicating may generate up to one
            # GatherDep instruction per worker. Multiple tasks from the same worker may
            # be clustered in the same instruction by _select_keys_for_gather. But once
            # a worker has been selected for a GatherDep and added to in_flight_workers,
            # it won't be selected again until the gather completes.
            instructions.append(
                GatherDep(
                    worker=worker,
                    to_gather=to_gather_keys,
                    total_nbytes=total_nbytes,
                    stimulus_id=stimulus_id,
                )
            )

            self.in_flight_workers[worker] = to_gather_keys
            self.transfer_incoming_count_total += 1
            self.transfer_incoming_bytes += total_nbytes
            if self._should_throttle_incoming_transfers():
                break

        return recommendations, instructions

    def _select_workers_for_gather(self) -> Iterator[tuple[str, HeapSet[TaskState]]]:
        """Helper of _ensure_communicating.

        Yield the peer workers and tasks in data_needed, sorted by:

        1. By highest-priority task available across all workers
        2. If tied, first by local peer workers, then remote. Note that, if a task is
           replicated across multiple host, it may go in a tie with itself.
        3. If still tied, by number of tasks available to be fetched from the host
           (see note below)
        4. If still tied, by a random element. This is statically seeded to guarantee
           reproducibility.

           FIXME https://github.com/dask/distributed/issues/6620
                 You won't get determinism when a single task is replicated on multiple
                 workers, because TaskState.who_has changes order at every interpreter
                 restart.

        Omit workers that are either busy or in flight.
        Remove peer workers with no tasks from data_needed.

        Note
        ----
        Instead of number of tasks, we could've measured total nbytes and/or number of
        tasks that only exist on the worker. Raw number of tasks is cruder but simpler.
        """
        host = get_address_host(self.address)
        heap = []

        for worker, tasks in list(self.data_needed.items()):
            if not tasks:
                del self.data_needed[worker]
                continue
            if worker in self.in_flight_workers or worker in self.busy_workers:
                continue
            heap.append(
                (
                    tasks.peek().priority,
                    get_address_host(worker) != host,  # False < True
                    -len(tasks),
                    self.rng.random(),
                    worker,
                    tasks,
                )
            )

        heapq.heapify(heap)
        while heap:
            _, is_remote, ntasks_neg, rnd, worker, tasks = heapq.heappop(heap)
            # The number of tasks and possibly the top priority task may have changed
            # since the last sort, since _select_keys_for_gather may have removed tasks
            # that are also replicated on a higher-priority worker.
            if not tasks:
                del self.data_needed[worker]
            elif -ntasks_neg != len(tasks):
                heapq.heappush(
                    heap,
                    (tasks.peek().priority, is_remote, -len(tasks), rnd, worker, tasks),
                )
            else:
                yield worker, tasks
                if not tasks:  # _select_keys_for_gather just emptied it
                    del self.data_needed[worker]

    def _select_keys_for_gather(
        self, available: HeapSet[TaskState]
    ) -> tuple[list[TaskState], int]:
        """Helper of _ensure_communicating.

        Fetch all tasks that are replicated on the target worker within a single
        message, up to transfer_message_target_bytes or until we reach the limit
        for the size of incoming data transfers.
        """
        to_gather: list[TaskState] = []
        total_nbytes = 0

        if self.transfer_incoming_bytes_limit is not None:
            bytes_left_to_fetch = min(
                self.transfer_incoming_bytes_limit - self.transfer_incoming_bytes,
                self.transfer_message_target_bytes,
            )
        else:
            bytes_left_to_fetch = self.transfer_message_target_bytes

        while available:
            ts = available.peek()
            if (
                # When there is no other traffic, the top-priority task is fetched
                # regardless of its size to ensure progress
                self.transfer_incoming_bytes
                or to_gather
            ) and total_nbytes + ts.get_nbytes() > bytes_left_to_fetch:
                break
            for worker in ts.who_has:
                # This also effectively pops from available
                self.data_needed[worker].remove(ts)
            to_gather.append(ts)
            total_nbytes += ts.get_nbytes()

        return to_gather, total_nbytes

    def _ensure_computing(self) -> RecsInstrs:
        if not self.running:
            return {}, []

        recs: Recs = {}
        while len(self.executing) < self.nthreads:
            ts = self._next_ready_task()
            if not ts:
                break

            if self.validate:
                assert ts.state in READY
                assert ts not in recs

            recs[ts] = "executing"
            self._acquire_resources(ts)
            self.executing.add(ts)

        return recs, []

    def _next_ready_task(self) -> TaskState | None:
        """Pop the top-priority task from self.ready or self.constrained"""
        if self.ready and self.constrained:
            tsr = self.ready.peek()
            tsc = self.constrained.peek()
            assert tsr.priority
            assert tsc.priority
            if tsc.priority < tsr.priority and self._resource_restrictions_satisfied(
                tsc
            ):
                return self.constrained.pop()
            else:
                return self.ready.pop()

        elif self.ready:
            return self.ready.pop()

        elif self.constrained:
            tsc = self.constrained.peek()
            if self._resource_restrictions_satisfied(tsc):
                return self.constrained.pop()

        return None

    def _get_task_finished_msg(
        self, ts: TaskState, stimulus_id: str
    ) -> TaskFinishedMsg:
        if ts.key not in self.data and ts.key not in self.actors:
            raise RuntimeError(f"Task {ts} not ready")
        typ = ts.type
        if ts.nbytes is None or typ is None:
            try:
                value = self.data[ts.key]
            except KeyError:
                value = self.actors[ts.key]
            ts.nbytes = sizeof(value)
            typ = ts.type = type(value)
            del value
        try:
            typ_serialized = pickle.dumps(typ, protocol=4)
        except Exception:
            # Some types fail pickling (example: _thread.lock objects),
            # send their name as a best effort.
            typ_serialized = pickle.dumps(typ.__name__, protocol=4)
        return TaskFinishedMsg(
            key=ts.key,
            nbytes=ts.nbytes,
            type=typ_serialized,
            typename=typename(typ),
            metadata=ts.metadata,
            thread=self.threads.get(ts.key),
            startstops=ts.startstops,
            stimulus_id=stimulus_id,
        )

    def _put_key_in_memory(
        self, ts: TaskState, value: object, *, stimulus_id: str
    ) -> Recs:
        """
        Put a key into memory and set data related task state attributes.
        On success, generate recommendations for dependents.

        This method does not generate any scheduler messages since this method
        cannot distinguish whether it has to be an `add-task` or a
        `task-finished` signal. The caller is required to generate this message
        on success.

        Raises
        ------
        Exception:
            In case the data is put into the in-memory buffer and a serialization error
            occurs during spilling, this re-raises that error. This has to be handled by
            the caller since most callers generate scheduler messages on success (see
            comment above) but we need to signal that this was not successful.

            Can only trigger if distributed.worker.memory.target is enabled, the value
            is individually larger than target * memory_limit, and the task is not an
            actor.
        """
        if ts.key in self.data:
            ts.state = "memory"
            return {}

        recommendations: Recs = {}
        if ts.key in self.actors:
            self.actors[ts.key] = value
        else:
            start = time()
            self.data[ts.key] = value
            stop = time()
            if stop - start > 0.020:
                ts.startstops.append(
                    {"action": "disk-write", "start": start, "stop": stop}
                )

        ts.state = "memory"
        if ts.nbytes is None:
            ts.nbytes = sizeof(value)

        ts.type = type(value)

        for dep in ts.dependents:
            dep.waiting_for_data.discard(ts)
            if not dep.waiting_for_data and dep.state == "waiting":
                self.waiting_for_data_count -= 1
                recommendations[dep] = "ready"

        self.log.append((ts.key, "put-in-memory", stimulus_id, time()))
        return recommendations

    ###############
    # Transitions #
    ###############

    def _transition_generic_fetch(self, ts: TaskState, stimulus_id: str) -> RecsInstrs:
        if not ts.who_has:
            return {ts: "missing"}, []

        ts.state = "fetch"
        ts.done = False
        assert ts.priority
        for w in ts.who_has:
            self.data_needed[w].add(ts)
        return {}, []

    def _transition_missing_waiting(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        self.missing_dep_flight.discard(ts)
        self._purge_state(ts)
        return self._transition_released_waiting(ts, stimulus_id=stimulus_id)

    def _transition_missing_fetch(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert ts.state == "missing"

        if not ts.who_has:
            return {}, []

        self.missing_dep_flight.discard(ts)
        return self._transition_generic_fetch(ts, stimulus_id=stimulus_id)

    def _transition_missing_released(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        self.missing_dep_flight.discard(ts)
        recs, instructions = self._transition_generic_released(
            ts, stimulus_id=stimulus_id
        )
        assert ts.key in self.tasks
        return recs, instructions

    def _transition_flight_missing(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        assert ts.done
        return self._transition_generic_missing(ts, stimulus_id=stimulus_id)

    def _transition_generic_missing(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert not ts.who_has

        ts.state = "missing"
        self.missing_dep_flight.add(ts)
        ts.done = False
        return {}, []

    def _transition_released_fetch(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert ts.state == "released"
        return self._transition_generic_fetch(ts, stimulus_id=stimulus_id)

    def _transition_generic_released(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        self._purge_state(ts)
        recs: Recs = {}
        for dependency in ts.dependencies:
            if (
                not dependency.waiters
                and dependency.state not in READY | PROCESSING | {"memory"}
            ):
                recs[dependency] = "released"

        ts.state = "released"
        if not ts.dependents:
            recs[ts] = "forgotten"

        return recs, []

    def _transition_released_waiting(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert all(d.key in self.tasks for d in ts.dependencies)

        recommendations: Recs = {}
        ts.waiting_for_data.clear()
        for dep_ts in ts.dependencies:
            if dep_ts.state != "memory":
                ts.waiting_for_data.add(dep_ts)
                dep_ts.waiters.add(ts)
                recommendations[dep_ts] = "fetch"

        if ts.waiting_for_data:
            self.waiting_for_data_count += 1
        else:
            recommendations[ts] = "ready"

        ts.state = "waiting"
        return recommendations, []

    def _transition_fetch_flight(
        self, ts: TaskState, worker: str, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert ts.state == "fetch"
            assert ts.who_has
            # The task has already been removed by _ensure_communicating
            for w in ts.who_has:
                assert ts not in self.data_needed[w]

        ts.done = False
        ts.state = "flight"
        ts.coming_from = worker
        self.in_flight_tasks.add(ts)
        return {}, []

    def _transition_memory_released(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        recs, instructions = self._transition_generic_released(
            ts, stimulus_id=stimulus_id
        )
        instructions.append(ReleaseWorkerDataMsg(key=ts.key, stimulus_id=stimulus_id))
        return recs, instructions

    def _transition_waiting_constrained(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert ts.state == "waiting"
            assert not ts.waiting_for_data
            assert all(
                dep.key in self.data or dep.key in self.actors
                for dep in ts.dependencies
            )
            assert all(dep.state == "memory" for dep in ts.dependencies)
            assert ts not in self.ready
            assert ts not in self.constrained
        ts.state = "constrained"
        self.constrained.add(ts)
        return self._ensure_computing()

    def _transition_executing_rescheduled(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        """Note: this transition is triggered exclusively by a task raising the
        Reschedule() Exception; it is not involved in work stealing.
        """
        assert ts.done
        return merge_recs_instructions(
            ({}, [RescheduleMsg(key=ts.key, stimulus_id=stimulus_id)]),
            # Note: this is not the same as recommending {ts: "released"} on the
            # previous line, as it would instead run the ("executing", "released")
            # transition, which would need special code for ts.done=True.
            self._transition_generic_released(ts, stimulus_id=stimulus_id),
        )

    def _transition_waiting_ready(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert ts.state == "waiting"
            assert ts not in self.ready
            assert ts not in self.constrained
            assert not ts.waiting_for_data
            for dep in ts.dependencies:
                assert dep.key in self.data or dep.key in self.actors
                assert dep.state == "memory"

        if ts.resource_restrictions:
            return {ts: "constrained"}, []

        ts.state = "ready"
        assert ts.priority is not None
        self.ready.add(ts)

        return self._ensure_computing()

    def _transition_generic_error(
        self,
        ts: TaskState,
        exception: Serialize,
        traceback: Serialize | None,
        exception_text: str,
        traceback_text: str,
        *,
        stimulus_id: str,
    ) -> RecsInstrs:
        ts.exception = exception
        ts.traceback = traceback
        ts.exception_text = exception_text
        ts.traceback_text = traceback_text
        ts.state = "error"
        smsg = TaskErredMsg.from_task(
            ts,
            stimulus_id=stimulus_id,
            thread=self.threads.get(ts.key),
        )

        return {}, [smsg]

    def _transition_resumed_error(
        self,
        ts: TaskState,
        exception: Serialize,
        traceback: Serialize | None,
        exception_text: str,
        traceback_text: str,
        *,
        stimulus_id: str,
    ) -> RecsInstrs:
        """In case of failure of the previous state, discard the error and kick off the
        next state without informing the scheduler
        """
        assert ts.done
        if ts.previous in ("executing", "long-running"):
            assert ts.next == "fetch"
            recs: Recs = {ts: "fetch"}
        else:
            assert ts.previous == "flight"
            assert ts.next == "waiting"
            recs = {ts: "waiting"}

        ts.state = "released"
        ts.done = False
        ts.previous = None
        ts.next = None
        return recs, []

    def _transition_resumed_rescheduled(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        """If the task raises the Reschedule() exception, but the scheduler already told
        the worker to fetch it somewhere else, silently transition to fetch.

        Note that this transition effectively duplicates the logic of
        _transition_resumed_error.
        """
        assert ts.done
        assert ts.previous in ("executing", "long-running")
        assert ts.next == "fetch"
        ts.state = "released"
        ts.done = False
        ts.previous = None
        ts.next = None
        return {ts: "fetch"}, []

    def _transition_resumed_fetch(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        """
        See also
        --------
        _transition_cancelled_fetch
        _transition_cancelled_waiting
        _transition_resumed_waiting
        _transition_flight_fetch
        """
        if ts.previous == "flight":
            if self.validate:
                assert ts.next == "waiting"
            if ts.done:
                # We arrived here either from GatherDepNetworkFailureEvent or from
                # GatherDepSuccessEvent but without the key in the data attribute.
                # We would now normally try to fetch the task from another peer worker
                # or transition it to missing if none are left; here instead we're going
                # to compute the task as we had been asked by the scheduler.
                ts.state = "released"
                ts.done = False
                ts.previous = None
                ts.next = None
                return {ts: "waiting"}, []
            else:
                # We're back where we started. We should forget about the entire
                # cancellation attempt
                ts.state = "flight"
                ts.previous = None
                ts.next = None

        elif self.validate:
            assert ts.previous in ("executing", "long-running")
            assert ts.next == "fetch"
            # None of the exit events of execute recommend a transition to fetch
            assert not ts.done

        return {}, []

    def _transition_resumed_missing(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        return {ts: "fetch"}, []

    def _transition_resumed_released(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        # None of the exit events of execute or gather_dep recommend a transition to
        # released
        assert not ts.done
        ts.state = "cancelled"
        ts.next = None
        return {}, []

    def _transition_resumed_waiting(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        """
        See also
        --------
        _transition_cancelled_fetch
        _transition_cancelled_or_resumed_long_running
        _transition_cancelled_waiting
        _transition_resumed_fetch
        """
        # None of the exit events of execute or gather_dep recommend a transition to
        # waiting
        assert not ts.done
        if ts.previous == "executing":
            assert ts.next == "fetch"
            # We're back where we started. We should forget about the entire
            # cancellation attempt
            ts.state = "executing"
            ts.next = None
            ts.previous = None
            return {}, []

        elif ts.previous == "long-running":
            assert ts.next == "fetch"
            # Same as executing, and in addition send the LongRunningMsg in arrears
            # Note that, if the task seceded before it was cancelled, this will cause
            # the message to be sent twice.
            ts.state = "long-running"
            ts.next = None
            ts.previous = None
            smsg = LongRunningMsg(
                key=ts.key, compute_duration=None, stimulus_id=stimulus_id
            )
            return {}, [smsg]

        else:
            assert ts.previous == "flight"
            assert ts.next == "waiting"
            return {}, []

    def _transition_cancelled_fetch(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        """
        See also
        --------
        _transition_cancelled_waiting
        _transition_resumed_fetch
        _transition_resumed_waiting
        """
        if ts.previous == "flight":
            if ts.done:
                # gather_dep just completed for a cancelled task.
                # Discard output and possibly forget
                return {ts: "released"}, []
            else:
                # Forget the task was cancelled to begin with
                ts.state = "flight"
                ts.previous = None
                return {}, []
        else:
            assert ts.previous in ("executing", "long-running")
            # None of the exit events of execute recommend a transition to fetch
            assert not ts.done
            ts.state = "resumed"
            ts.next = "fetch"
            return {}, []

    def _transition_cancelled_waiting(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        """
        See also
        --------
        _transition_cancelled_fetch
        _transition_cancelled_or_resumed_long_running
        _transition_resumed_fetch
        _transition_resumed_waiting
        """
        # None of the exit events of gather_dep or execute recommend a transition to
        # waiting
        assert not ts.done
        if ts.previous == "executing":
            # Forget the task was cancelled to begin with
            ts.state = "executing"
            ts.previous = None
            return {}, []
        elif ts.previous == "long-running":
            # Forget the task was cancelled to begin with, and inform the scheduler
            # in arrears that it has seceded.
            # Note that, if the task seceded before it was cancelled, this will cause
            # the message to be sent twice.
            ts.state = "long-running"
            ts.previous = None
            smsg = LongRunningMsg(
                key=ts.key, compute_duration=None, stimulus_id=stimulus_id
            )
            return {}, [smsg]
        else:
            assert ts.previous == "flight"
            ts.state = "resumed"
            ts.next = "waiting"
            return {}, []

    def _transition_cancelled_released(
        self,
        ts: TaskState,
        *args: Any,  # extra arguments of transitions to memory or error - ignored
        stimulus_id: str,
    ) -> RecsInstrs:
        if not ts.done:
            return {}, []

        ts.previous = None
        ts.done = False
        return self._transition_generic_released(ts, stimulus_id=stimulus_id)

    def _transition_executing_released(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        """We can't stop executing a task just because the scheduler asked us to,
        so we're entering cancelled state and waiting until it completes.
        """
        if self.validate:
            assert ts.state in ("executing", "long-running")
            assert not ts.next
            assert not ts.done
        ts.previous = cast(Literal["executing", "long-running"], ts.state)
        ts.state = "cancelled"
        return {}, []

    def _transition_constrained_executing(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert ts.state == "constrained"
            assert not ts.waiting_for_data
            assert ts.key not in self.data
            assert ts not in self.ready
            assert ts not in self.constrained
            for dep in ts.dependencies:
                assert dep.key in self.data or dep.key in self.actors

        ts.state = "executing"
        instr = Execute(key=ts.key, stimulus_id=stimulus_id)
        return {}, [instr]

    def _transition_ready_executing(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        if self.validate:
            assert ts.state == "ready"
            assert not ts.waiting_for_data
            assert ts.key not in self.data
            assert ts not in self.ready
            assert ts not in self.constrained
            assert all(
                dep.key in self.data or dep.key in self.actors
                for dep in ts.dependencies
            )

        ts.state = "executing"
        instr = Execute(key=ts.key, stimulus_id=stimulus_id)
        return {}, [instr]

    def _transition_flight_fetch(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        # If this transition is called after the flight coroutine has finished,
        # we can reset the task and transition to fetch again. If it is not yet
        # finished, this should be a no-op
        if not ts.done:
            return {}, []

        return self._transition_generic_fetch(ts, stimulus_id=stimulus_id)

    def _transition_flight_released(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        # None of the exit events of gather_dep recommend a transition to released
        assert not ts.done
        ts.previous = "flight"
        ts.next = None
        # See https://github.com/dask/distributed/pull/5046#discussion_r685093940
        ts.state = "cancelled"
        return {}, []

    def _transition_executing_long_running(
        self, ts: TaskState, compute_duration: float, *, stimulus_id: str
    ) -> RecsInstrs:
        """
        See also
        --------
        _transition_cancelled_or_resumed_long_running
        """
        ts.state = "long-running"
        self.executing.discard(ts)
        self.long_running.add(ts)

        smsg = LongRunningMsg(
            key=ts.key, compute_duration=compute_duration, stimulus_id=stimulus_id
        )
        return merge_recs_instructions(
            ({}, [smsg]),
            self._ensure_computing(),
        )

    def _transition_cancelled_or_resumed_long_running(
        self, ts: TaskState, compute_duration: float, *, stimulus_id: str
    ) -> RecsInstrs:
        """Handles transitions:

        - cancelled(executing) -> long-running
        - cancelled(long-running) -> long-running (user called secede() twice)
        - resumed(executing->fetch) -> long-running
        - resumed(long-running->fetch) -> long-running (user called secede() twice)

        Unlike in the executing->long_running transition, do not send LongRunningMsg.
        From the scheduler's perspective, this task no longer exists (cancelled) or is
        in memory on another worker (resumed). So it shouldn't hear about it.
        Instead, we're going to send the LongRunningMsg when and if the task
        transitions back to waiting.

        See also
        --------
        _transition_executing_long_running
        _transition_cancelled_waiting
        _transition_resumed_waiting
        """
        assert ts.previous in ("executing", "long-running")
        ts.previous = "long-running"
        self.executing.discard(ts)
        self.long_running.add(ts)
        return self._ensure_computing()

    def _transition_executing_memory(
        self, ts: TaskState, value: object, *, stimulus_id: str
    ) -> RecsInstrs:
        """This transition is *normally* triggered by ExecuteSuccessEvent.
        However, beware that it can also be triggered by scatter().
        """
        return self._transition_to_memory(
            ts, value, "task-finished", stimulus_id=stimulus_id
        )

    def _transition_released_memory(
        self, ts: TaskState, value: object, *, stimulus_id: str
    ) -> RecsInstrs:
        """This transition is triggered by scatter()"""
        return self._transition_to_memory(
            ts, value, "add-keys", stimulus_id=stimulus_id
        )

    def _transition_flight_memory(
        self, ts: TaskState, value: object, *, stimulus_id: str
    ) -> RecsInstrs:
        """This transition is *normally* triggered by GatherDepSuccessEvent.
        However, beware that it can also be triggered by scatter().
        """
        return self._transition_to_memory(
            ts, value, "add-keys", stimulus_id=stimulus_id
        )

    def _transition_resumed_memory(
        self, ts: TaskState, value: object, *, stimulus_id: str
    ) -> RecsInstrs:
        """Normally, we send to the scheduler a 'task-finished' message for a completed
        execution and 'add-data' for a completed replication from another worker. The
        scheduler's reaction to the two messages is fundamentally different; namely,
        add-data is only admissible for tasks that are already in memory on another
        worker, and won't trigger transitions.

        In the case of resumed tasks, the scheduler's expectation is set by ts.next -
        which means, the opposite of what the worker actually just completed.
        """
        msg_type: Literal["add-keys", "task-finished"]
        if ts.previous in ("executing", "long-running"):
            assert ts.next == "fetch"
            msg_type = "add-keys"
        else:
            assert ts.previous == "flight"
            assert ts.next == "waiting"
            msg_type = "task-finished"

        ts.previous = None
        ts.next = None
        return self._transition_to_memory(ts, value, msg_type, stimulus_id=stimulus_id)

    def _transition_to_memory(
        self,
        ts: TaskState,
        value: object,
        msg_type: Literal["add-keys", "task-finished"],
        *,
        stimulus_id: str,
    ) -> RecsInstrs:
        try:
            recs = self._put_key_in_memory(ts, value, stimulus_id=stimulus_id)
        except Exception as e:
            msg = error_message(e)
            recs = {ts: tuple(msg.values())}
            return recs, []

        # NOTE: The scheduler's reaction to these two messages is fundamentally
        # different. Namely, add-keys is only admissible for tasks that are already in
        # memory on another worker, and won't trigger transitions.
        if msg_type == "add-keys":
            smsg: Instruction = AddKeysMsg(keys=[ts.key], stimulus_id=stimulus_id)
        else:
            assert msg_type == "task-finished"
            smsg = self._get_task_finished_msg(ts, stimulus_id=stimulus_id)
        return recs, [smsg]

    def _transition_released_forgotten(
        self, ts: TaskState, *, stimulus_id: str
    ) -> RecsInstrs:
        recommendations: Recs = {}
        # Dependents _should_ be released by the scheduler before this
        if self.validate:
            assert not any(d.state != "forgotten" for d in ts.dependents)
        for dep in ts.dependencies:
            dep.dependents.discard(ts)
            if dep.state == "released" and not dep.dependents:
                recommendations[dep] = "forgotten"
        self._purge_state(ts)
        # Mark state as forgotten in case it is still referenced
        ts.state = "forgotten"
        self.tasks.pop(ts.key, None)
        return recommendations, []

    # {
    #     (start, finish):
    #     transition_<start>_<finish>(
    #         self, ts: TaskState, *args, stimulus_id: str
    #     ) -> (recommendations, instructions)
    # }
    _TRANSITIONS_TABLE: ClassVar[
        Mapping[tuple[TaskStateState, TaskStateState], Callable[..., RecsInstrs]]
    ] = {
        ("cancelled", "error"): _transition_cancelled_released,
        ("cancelled", "fetch"): _transition_cancelled_fetch,
        ("cancelled", "long-running"): _transition_cancelled_or_resumed_long_running,
        ("cancelled", "memory"): _transition_cancelled_released,
        ("cancelled", "missing"): _transition_cancelled_released,
        ("cancelled", "released"): _transition_cancelled_released,
        ("cancelled", "rescheduled"): _transition_cancelled_released,
        ("cancelled", "waiting"): _transition_cancelled_waiting,
        ("resumed", "error"): _transition_resumed_error,
        ("resumed", "fetch"): _transition_resumed_fetch,
        ("resumed", "long-running"): _transition_cancelled_or_resumed_long_running,
        ("resumed", "memory"): _transition_resumed_memory,
        ("resumed", "released"): _transition_resumed_released,
        ("resumed", "rescheduled"): _transition_resumed_rescheduled,
        ("resumed", "waiting"): _transition_resumed_waiting,
        ("constrained", "executing"): _transition_constrained_executing,
        ("constrained", "released"): _transition_generic_released,
        ("error", "released"): _transition_generic_released,
        ("executing", "error"): _transition_generic_error,
        ("executing", "long-running"): _transition_executing_long_running,
        ("executing", "memory"): _transition_executing_memory,
        ("executing", "released"): _transition_executing_released,
        ("executing", "rescheduled"): _transition_executing_rescheduled,
        ("fetch", "flight"): _transition_fetch_flight,
        ("fetch", "missing"): _transition_generic_missing,
        ("fetch", "released"): _transition_generic_released,
        ("flight", "error"): _transition_generic_error,
        ("flight", "fetch"): _transition_flight_fetch,
        ("flight", "memory"): _transition_flight_memory,
        ("flight", "missing"): _transition_flight_missing,
        ("flight", "released"): _transition_flight_released,
        ("long-running", "error"): _transition_generic_error,
        ("long-running", "memory"): _transition_executing_memory,
        ("long-running", "rescheduled"): _transition_executing_rescheduled,
        ("long-running", "released"): _transition_executing_released,
        ("memory", "released"): _transition_memory_released,
        ("missing", "error"): _transition_generic_error,
        ("missing", "fetch"): _transition_missing_fetch,
        ("missing", "released"): _transition_missing_released,
        ("missing", "waiting"): _transition_missing_waiting,
        ("ready", "executing"): _transition_ready_executing,
        ("ready", "released"): _transition_generic_released,
        ("released", "error"): _transition_generic_error,
        ("released", "fetch"): _transition_released_fetch,
        ("released", "forgotten"): _transition_released_forgotten,
        ("released", "memory"): _transition_released_memory,
        ("released", "missing"): _transition_generic_missing,
        ("released", "waiting"): _transition_released_waiting,
        ("waiting", "constrained"): _transition_waiting_constrained,
        ("waiting", "ready"): _transition_waiting_ready,
        ("waiting", "released"): _transition_generic_released,
    }

    def _notify_plugins(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        for name, plugin in self.plugins.items():
            if hasattr(plugin, method_name):
                try:
                    getattr(plugin, method_name)(*args, **kwargs)
                except Exception:
                    logger.info(
                        "Plugin '%s' failed with exception", name, exc_info=True
                    )

    def _transition(
        self,
        ts: TaskState,
        finish: TaskStateState | tuple,
        *args: Any,
        stimulus_id: str,
    ) -> RecsInstrs:
        """Transition a key from its current state to the finish state

        See Also
        --------
        Worker.transitions: wrapper around this method
        """
        if isinstance(finish, tuple):
            # the concatenated transition path might need to access the tuple
            assert not args
            args = finish[1:]
            finish = cast(TaskStateState, finish[0])

        if ts.state == finish:
            return {}, []

        start = ts.state
        func = self._TRANSITIONS_TABLE.get((start, finish))

        # Notes:
        # - in case of transition through released, this counter is incremented by 2
        # - this increase happens before the actual transitions, so that it can
        #   catch potential infinite recursions
        self.transition_counter += 1
        if (
            self.transition_counter_max
            and self.transition_counter >= self.transition_counter_max
        ):
            raise TransitionCounterMaxExceeded(ts.key, start, finish, self.story(ts))

        if func is not None:
            recs, instructions = func(self, ts, *args, stimulus_id=stimulus_id)
            self._notify_plugins("transition", ts.key, start, finish)

        elif "released" not in (start, finish):
            # start -> "released" -> finish
            try:
                recs, instructions = self._transition(
                    ts, "released", stimulus_id=stimulus_id
                )
                v_state: TaskStateState
                v_args: list | tuple
                while v := recs.pop(ts, None):
                    if isinstance(v, tuple):
                        v_state, *v_args = v
                    else:
                        v_state, v_args = v, ()
                    if v_state == "forgotten":
                        # We do not want to forget. The purpose of this
                        # transition path is to get to `finish`
                        continue
                    recs, instructions = merge_recs_instructions(
                        (recs, instructions),
                        self._transition(ts, v_state, *v_args, stimulus_id=stimulus_id),
                    )
                recs, instructions = merge_recs_instructions(
                    (recs, instructions),
                    self._transition(ts, finish, *args, stimulus_id=stimulus_id),
                )
            except (InvalidTransition, RecommendationsConflict) as e:
                raise InvalidTransition(ts.key, start, finish, self.story(ts)) from e

        else:
            raise InvalidTransition(ts.key, start, finish, self.story(ts))

        self.log.append(
            (
                # key
                ts.key,
                # initial
                start,
                # recommended
                finish,
                # final
                ts.state,
                # new recommendations
                {
                    ts.key: new[0] if isinstance(new, tuple) else new
                    for ts, new in recs.items()
                },
                stimulus_id,
                time(),
            )
        )
        return recs, instructions

    def _resource_restrictions_satisfied(self, ts: TaskState) -> bool:
        return all(
            self.available_resources[resource] >= needed
            for resource, needed in ts.resource_restrictions.items()
        )

    def _acquire_resources(self, ts: TaskState) -> None:
        for resource, needed in ts.resource_restrictions.items():
            self.available_resources[resource] -= needed

    def _release_resources(self, ts: TaskState) -> None:
        for resource, needed in ts.resource_restrictions.items():
            self.available_resources[resource] += needed

    def _transitions(self, recommendations: Recs, *, stimulus_id: str) -> Instructions:
        """Process transitions until none are left

        This includes feedback from previous transitions and continues until we
        reach a steady state
        """
        instructions = []
        tasks = set()

        def process_recs(recs: Recs) -> None:
            while recs:
                ts, finish = recs.popitem()
                tasks.add(ts)
                a_recs, a_instructions = self._transition(
                    ts, finish, stimulus_id=stimulus_id
                )
                recs.update(a_recs)
                instructions.extend(a_instructions)

        process_recs(recommendations.copy())

        # We could call _ensure_communicating after we change something that could
        # trigger a new call to gather_dep (e.g. on transitions to fetch,
        # GatherDepDoneEvent, or RetryBusyWorkerEvent). However, doing so we'd
        # potentially call it too early, before all tasks have transitioned to fetch.
        # This in turn would hurt aggregation of multiple tasks into a single GatherDep
        # instruction.
        # Read: https://github.com/dask/distributed/issues/6497
        a_recs, a_instructions = self._ensure_communicating(stimulus_id=stimulus_id)
        instructions += a_instructions
        process_recs(a_recs)

        if self.validate:
            # Full state validation is very expensive
            for ts in tasks:
                self.validate_task(ts)

        return instructions

    ##########
    # Events #
    ##########

    @singledispatchmethod
    def _handle_event(self, ev: StateMachineEvent) -> RecsInstrs:
        raise TypeError(ev)  # pragma: nocover

    @_handle_event.register
    def _handle_update_data(self, ev: UpdateDataEvent) -> RecsInstrs:
        recommendations: Recs = {}
        instructions: Instructions = []
        for key, value in ev.data.items():
            try:
                ts = self.tasks[key]
                recommendations[ts] = ("memory", value)
            except KeyError:
                self.tasks[key] = ts = TaskState(key)

                try:
                    recs = self._put_key_in_memory(
                        ts, value, stimulus_id=ev.stimulus_id
                    )
                except Exception as e:
                    msg = error_message(e)
                    recommendations = {ts: tuple(msg.values())}
                else:
                    recommendations.update(recs)

            self.log.append((key, "receive-from-scatter", ev.stimulus_id, time()))

        if ev.report:
            instructions.append(
                AddKeysMsg(keys=list(ev.data), stimulus_id=ev.stimulus_id)
            )

        return recommendations, instructions

    @_handle_event.register
    def _handle_free_keys(self, ev: FreeKeysEvent) -> RecsInstrs:
        """Handler to be called by the scheduler.

        The given keys are no longer referred to and required by the scheduler.
        The worker is now allowed to release the key, if applicable.

        This does not guarantee that the memory is released since the worker may
        still decide to hold on to the data and task since it is required by an
        upstream dependency.
        """
        self.log.append(("free-keys", ev.keys, ev.stimulus_id, time()))
        recommendations: Recs = {}
        for key in ev.keys:
            ts = self.tasks.get(key)
            if ts:
                recommendations[ts] = "released"
        return recommendations, []

    @_handle_event.register
    def _handle_remove_replicas(self, ev: RemoveReplicasEvent) -> RecsInstrs:
        """Stream handler notifying the worker that it might be holding unreferenced,
        superfluous data.

        This should not actually happen during ordinary operations and is only intended
        to correct any erroneous state. An example where this is necessary is if a
        worker fetches data for a downstream task but that task is released before the
        data arrives. In this case, the scheduler will notify the worker that it may be
        holding this unnecessary data, if the worker hasn't released the data itself,
        already.

        This handler does not guarantee the task nor the data to be actually
        released but only asks the worker to release the data on a best effort
        guarantee. This protects from race conditions where the given keys may
        already have been rescheduled for compute in which case the compute
        would win and this handler is ignored.

        For stronger guarantees, see handler free_keys
        """
        recommendations: Recs = {}
        instructions: Instructions = []

        rejected = []
        for key in ev.keys:
            ts = self.tasks.get(key)
            if ts is None or ts.state != "memory":
                continue
            if not ts.is_protected():
                self.log.append(
                    (ts.key, "remove-replica-confirmed", ev.stimulus_id, time())
                )
                recommendations[ts] = "released"
            else:
                rejected.append(key)

        if rejected:
            self.log.append(
                ("remove-replica-rejected", rejected, ev.stimulus_id, time())
            )
            instructions.append(AddKeysMsg(keys=rejected, stimulus_id=ev.stimulus_id))

        return recommendations, instructions

    @_handle_event.register
    def _handle_acquire_replicas(self, ev: AcquireReplicasEvent) -> RecsInstrs:
        if self.validate:
            assert ev.who_has.keys() == ev.nbytes.keys()
            assert all(ev.who_has.values())

        recommendations: Recs = {}
        for key, nbytes in ev.nbytes.items():
            ts = self._ensure_task_exists(
                key=key,
                # Transfer this data after all dependency tasks of computations with
                # default or explicitly high (>0) user priority and before all
                # computations with low priority (<0). Note that the priority= parameter
                # of compute() is multiplied by -1 before it reaches TaskState.priority.
                priority=(1,),
                stimulus_id=ev.stimulus_id,
            )
            if ts.state != "memory":
                ts.nbytes = nbytes
                recommendations[ts] = "fetch"

        self._update_who_has(ev.who_has)
        return recommendations, []

    @_handle_event.register
    def _handle_compute_task(self, ev: ComputeTaskEvent) -> RecsInstrs:
        try:
            ts = self.tasks[ev.key]
            logger.debug(
                "Asked to compute an already known task %s",
                {"task": ts, "stimulus_id": ev.stimulus_id},
            )
        except KeyError:
            self.tasks[ev.key] = ts = TaskState(ev.key)
        self.log.append((ev.key, "compute-task", ts.state, ev.stimulus_id, time()))

        recommendations: Recs = {}
        instructions: Instructions = []

        if ts.state in READY | {
            "executing",
            "long-running",
            "waiting",
        }:
            pass
        elif ts.state == "memory":
            instructions.append(
                self._get_task_finished_msg(ts, stimulus_id=ev.stimulus_id)
            )
        elif ts.state == "error":
            instructions.append(TaskErredMsg.from_task(ts, stimulus_id=ev.stimulus_id))
        elif ts.state in {
            "released",
            "fetch",
            "flight",
            "missing",
            "cancelled",
            "resumed",
        }:
            recommendations[ts] = "waiting"

            ts.run_spec = ev.run_spec

            priority = ev.priority + (self.generation,)
            self.generation -= 1

            if ev.actor:
                self.actors[ts.key] = None

            ts.exception = None
            ts.traceback = None
            ts.exception_text = ""
            ts.traceback_text = ""
            ts.priority = priority
            ts.duration = ev.duration
            ts.annotations = ev.annotations

            # If we receive ComputeTaskEvent twice for the same task, resources may have
            # changed, but the task is still running. Preserve the previous resource
            # restrictions so that they can be properly released when it eventually
            # completes.
            if not (
                ts.state in ("cancelled", "resumed")
                and ts.previous in ("executing", "long-running")
            ):
                ts.resource_restrictions = ev.resource_restrictions

            if self.validate:
                assert ev.who_has.keys() == ev.nbytes.keys()
                for dep_workers in ev.who_has.values():
                    assert dep_workers
                    assert len(dep_workers) == len(set(dep_workers))

            for dep_key, nbytes in ev.nbytes.items():
                dep_ts = self._ensure_task_exists(
                    key=dep_key,
                    priority=priority,
                    stimulus_id=ev.stimulus_id,
                )
                self.tasks[dep_key].nbytes = nbytes

                # link up to child / parents
                ts.dependencies.add(dep_ts)
                dep_ts.dependents.add(ts)

            self._update_who_has(ev.who_has)
        else:
            raise RuntimeError(  # pragma: nocover
                f"Unexpected task state encountered for {ts}; "
                f"stimulus_id={ev.stimulus_id}; story={self.story(ts)}"
            )

        return recommendations, instructions

    def _gather_dep_done_common(self, ev: GatherDepDoneEvent) -> Iterator[TaskState]:
        """Common code for the handlers of all subclasses of GatherDepDoneEvent.

        Yields the tasks that need to transition out of flight.
        The task states can be flight, cancelled, or resumed, but in case of scatter()
        they can also be in memory or error states.

        See also
        --------
        _execute_done_common
        """
        self.transfer_incoming_bytes -= ev.total_nbytes
        keys = self.in_flight_workers.pop(ev.worker)
        for key in keys:
            ts = self.tasks[key]
            ts.done = True
            ts.coming_from = None
            self.in_flight_tasks.remove(ts)
            yield ts

    @_handle_event.register
    def _handle_gather_dep_success(self, ev: GatherDepSuccessEvent) -> RecsInstrs:
        """gather_dep terminated successfully.
        The response may contain less keys than the request.
        """
        recommendations: Recs = {}
        for ts in self._gather_dep_done_common(ev):
            if ts.key in ev.data:
                recommendations[ts] = ("memory", ev.data[ts.key])
            else:
                self.log.append((ts.key, "missing-dep", ev.stimulus_id, time()))
                if self.validate:
                    assert ts.state != "fetch"
                    assert ts not in self.data_needed[ev.worker]
                ts.who_has.discard(ev.worker)
                self.has_what[ev.worker].discard(ts.key)
                recommendations[ts] = "fetch"

        return recommendations, []

    @_handle_event.register
    def _handle_gather_dep_busy(self, ev: GatherDepBusyEvent) -> RecsInstrs:
        """gather_dep terminated: remote worker is busy"""
        # Avoid hammering the worker. If there are multiple replicas
        # available, immediately try fetching from a different worker.
        self.busy_workers.add(ev.worker)

        recommendations: Recs = {}
        refresh_who_has = []
        for ts in self._gather_dep_done_common(ev):
            recommendations[ts] = "fetch"
            if not ts.who_has - self.busy_workers:
                refresh_who_has.append(ts.key)

        instructions: Instructions = [
            RetryBusyWorkerLater(worker=ev.worker, stimulus_id=ev.stimulus_id),
        ]

        if refresh_who_has:
            # All workers that hold known replicas of our tasks are busy.
            # Try querying the scheduler for unknown ones.
            instructions.append(
                RequestRefreshWhoHasMsg(
                    keys=refresh_who_has, stimulus_id=ev.stimulus_id
                )
            )

        return recommendations, instructions

    @_handle_event.register
    def _handle_gather_dep_network_failure(
        self, ev: GatherDepNetworkFailureEvent
    ) -> RecsInstrs:
        """gather_dep terminated: network failure while trying to
        communicate with remote worker

        Though the network failure could be transient, we assume it is not, and
        preemptively act as though the other worker has died (including removing all
        keys from it, even ones we did not fetch).

        This optimization leads to faster completion of the fetch, since we immediately
        either retry a different worker, or ask the scheduler to inform us of a new
        worker if no other worker is available.
        """
        recommendations: Recs = {}

        for ts in self._gather_dep_done_common(ev):
            self.log.append((ts.key, "missing-dep", ev.stimulus_id, time()))
            recommendations[ts] = "fetch"

        for ts in self.data_needed.pop(ev.worker, ()):
            if self.validate:
                assert ts.state == "fetch"
                assert ev.worker in ts.who_has
            if ts.who_has == {ev.worker}:
                # This can override a recommendation from the previous for loop
                recommendations[ts] = "missing"

        for key in self.has_what.pop(ev.worker):
            ts = self.tasks[key]
            ts.who_has.remove(ev.worker)

        return recommendations, []

    @_handle_event.register
    def _handle_gather_dep_failure(self, ev: GatherDepFailureEvent) -> RecsInstrs:
        """gather_dep terminated: generic error raised (not a network failure);
        e.g. data failed to deserialize.
        """
        recommendations: Recs = {
            ts: (
                "error",
                ev.exception,
                ev.traceback,
                ev.exception_text,
                ev.traceback_text,
            )
            for ts in self._gather_dep_done_common(ev)
        }

        return recommendations, []

    @_handle_event.register
    def _handle_secede(self, ev: SecedeEvent) -> RecsInstrs:
        ts = self.tasks.get(ev.key)
        if not ts:
            return {}, []
        return {ts: ("long-running", ev.compute_duration)}, []

    @_handle_event.register
    def _handle_steal_request(self, ev: StealRequestEvent) -> RecsInstrs:
        # There may be a race condition between stealing and releasing a task.
        # In this case the self.tasks is already cleared. The `None` will be
        # registered as `already-computing` on the other end
        ts = self.tasks.get(ev.key)
        state = ts.state if ts is not None else None
        smsg = StealResponseMsg(key=ev.key, state=state, stimulus_id=ev.stimulus_id)

        if state in READY | {"waiting"}:
            # If task is marked as "constrained" we haven't yet assigned it an
            # `available_resources` to run on, that happens in
            # `_transition_constrained_executing`
            assert ts
            return {ts: "released"}, [smsg]
        else:
            return {}, [smsg]

    @_handle_event.register
    def _handle_pause(self, ev: PauseEvent) -> RecsInstrs:
        """Prevent any further tasks to be executed or gathered. Tasks that are
        currently executing or in flight will continue to progress.
        """
        self.running = False
        return {}, []

    @_handle_event.register
    def _handle_unpause(self, ev: UnpauseEvent) -> RecsInstrs:
        """Emerge from paused status"""
        self.running = True
        return self._ensure_computing()

    @_handle_event.register
    def _handle_retry_busy_worker(self, ev: RetryBusyWorkerEvent) -> RecsInstrs:
        self.busy_workers.discard(ev.worker)
        return {}, []

    @_handle_event.register
    def _handle_cancel_compute(self, ev: CancelComputeEvent) -> RecsInstrs:
        """Cancel a task on a best-effort basis. This is only possible while a task
        is in state `waiting` or `ready`; nothing will happen otherwise.
        """
        ts = self.tasks.get(ev.key)
        if not ts or ts.state not in READY | {"waiting"}:
            return {}, []

        self.log.append((ev.key, "cancel-compute", ev.stimulus_id, time()))
        # All possible dependents of ts should not be in state Processing on
        # scheduler side and therefore should not be assigned to a worker, yet.
        assert not ts.dependents
        return {ts: "released"}, []

    def _execute_done_common(
        self, ev: ExecuteDoneEvent
    ) -> tuple[TaskState, Recs, Instructions]:
        """Common code for the handlers of all subclasses of ExecuteDoneEvent.

        The task state can be executing, cancelled, or resumed, but in case of scatter()
        it can also be in memory or error state.

        See also
        --------
        _gather_dep_done_common
        """
        # key *must* be still in tasks - see _transition_released_forgotten
        ts = self.tasks.get(ev.key)
        assert ts, self.story(ev.key)
        if self.validate:
            assert (ts in self.executing) != (ts in self.long_running)  # XOR
        ts.done = True

        self.executed_count += 1
        self._release_resources(ts)
        self.executing.discard(ts)
        self.long_running.discard(ts)

        recs, instr = self._ensure_computing()
        assert ts not in recs
        return ts, recs, instr

    @_handle_event.register
    def _handle_execute_success(self, ev: ExecuteSuccessEvent) -> RecsInstrs:
        """Task completed successfully"""
        ts, recs, instr = self._execute_done_common(ev)
        ts.startstops.append({"action": "compute", "start": ev.start, "stop": ev.stop})
        ts.nbytes = ev.nbytes
        ts.type = ev.type
        recs[ts] = ("memory", ev.value)
        return recs, instr

    @_handle_event.register
    def _handle_execute_failure(self, ev: ExecuteFailureEvent) -> RecsInstrs:
        """Task execution failed"""
        ts, recs, instr = self._execute_done_common(ev)
        if ev.start is not None and ev.stop is not None:
            ts.startstops.append(
                {"action": "compute", "start": ev.start, "stop": ev.stop}
            )
        recs[ts] = (
            "error",
            ev.exception,
            ev.traceback,
            ev.exception_text,
            ev.traceback_text,
        )
        return recs, instr

    @_handle_event.register
    def _handle_reschedule(self, ev: RescheduleEvent) -> RecsInstrs:
        """Task raised Reschedule() exception while it was running.

        Note: this has nothing to do with work stealing, which instead causes a
        FreeKeysEvent.
        """
        ts, recs, instr = self._execute_done_common(ev)
        recs[ts] = "rescheduled"
        return recs, instr

    @_handle_event.register
    def _handle_find_missing(self, ev: FindMissingEvent) -> RecsInstrs:
        if not self.missing_dep_flight:
            return {}, []

        if self.validate:
            for ts in self.missing_dep_flight:
                assert not ts.who_has, self.story(ts)

        smsg = RequestRefreshWhoHasMsg(
            keys=[ts.key for ts in self.missing_dep_flight],
            stimulus_id=ev.stimulus_id,
        )
        return {}, [smsg]

    @_handle_event.register
    def _handle_refresh_who_has(self, ev: RefreshWhoHasEvent) -> RecsInstrs:
        self._update_who_has(ev.who_has)
        recommendations: Recs = {}
        instructions: Instructions = []

        for key in ev.who_has:
            ts = self.tasks.get(key)
            if not ts:
                continue

            if ts.who_has and ts.state == "missing":
                recommendations[ts] = "fetch"
            elif not ts.who_has and ts.state == "fetch":
                recommendations[ts] = "missing"
            # Note: if ts.who_has and ts.state == "fetch", we may have just acquired new
            # replicas whereas all previously known workers are in flight or busy. We
            # rely on _transitions to call _ensure_communicating every time, even in
            # absence of recommendations, to potentially kick off a new call to
            # gather_dep.

        return recommendations, instructions

    ###############
    # Diagnostics #
    ###############

    def story(self, *keys_or_tasks_or_stimuli: str | TaskState) -> list[tuple]:
        """Return all records from the transitions log involving one or more tasks or
        stimulus_id's
        """
        keys_or_stimuli = {
            e.key if isinstance(e, TaskState) else e for e in keys_or_tasks_or_stimuli
        }
        return worker_story(keys_or_stimuli, self.log)

    def stimulus_story(
        self, *keys_or_tasks: str | TaskState
    ) -> list[StateMachineEvent]:
        """Return all state machine events involving one or more tasks"""
        keys = {e.key if isinstance(e, TaskState) else e for e in keys_or_tasks}
        return [ev for ev in self.stimulus_log if getattr(ev, "key", None) in keys]

    def _to_dict(self, *, exclude: Container[str] = ()) -> dict:
        """Dictionary representation for debugging purposes.
        Not type stable and not intended for roundtrips.

        See also
        --------
        Client.dump_cluster_state
        distributed.utils.recursive_to_dict
        """
        info = {
            "address": self.address,
            "nthreads": self.nthreads,
            "running": self.running,
            "ready": [ts.key for ts in self.ready.sorted()],
            "constrained": [ts.key for ts in self.constrained.sorted()],
            "data": dict.fromkeys(self.data),
            "data_needed": {
                w: [ts.key for ts in tss.sorted()]
                for w, tss in self.data_needed.items()
            },
            "executing": {ts.key for ts in self.executing},
            "has_what": dict(self.has_what),
            "long_running": {ts.key for ts in self.long_running},
            "in_flight_tasks": {ts.key for ts in self.in_flight_tasks},
            "in_flight_workers": self.in_flight_workers,
            "missing_dep_flight": [ts.key for ts in self.missing_dep_flight],
            "busy_workers": self.busy_workers,
            "log": self.log,
            "stimulus_log": self.stimulus_log,
            "transition_counter": self.transition_counter,
            "tasks": self.tasks,
        }
        info = {k: v for k, v in info.items() if k not in exclude}
        return recursive_to_dict(info, exclude=exclude)

    ##############
    # Validation #
    ##############

    def _validate_task_memory(self, ts: TaskState) -> None:
        assert ts.key in self.data or ts.key in self.actors
        assert isinstance(ts.nbytes, int)
        assert not ts.waiting_for_data

    def _validate_task_executing(self, ts: TaskState) -> None:
        """Validate tasks:

        - ts.state == executing
        - ts.state == long-running
        - ts.state == cancelled, ts.previous == executing
        - ts.state == cancelled, ts.previous == long-running
        - ts.state == resumed, ts.previous == executing, ts.next == fetch
        - ts.state == resumed, ts.previous == long-running, ts.next == fetch
        """
        if ts.state == "executing" or ts.previous == "executing":
            assert ts in self.executing
            assert ts not in self.long_running
        else:
            assert ts.state == "long-running" or ts.previous == "long-running"
            assert ts not in self.executing
            assert ts in self.long_running

        assert ts.run_spec is not None
        assert ts.key not in self.data
        assert not ts.waiting_for_data

        # FIXME https://github.com/dask/distributed/issues/6893
        # This assertion can be false for
        # - cancelled or resumed tasks
        # - executing tasks which used to be cancelled in the past
        # for dep in ts.dependencies:
        #     assert dep.state == "memory", self.story(dep)
        #     assert dep.key in self.data or dep.key in self.actors

    def _validate_task_ready(self, ts: TaskState) -> None:
        """Validate tasks:

        - ts.state == ready
        - ts.state == constrained
        """
        if ts.state == "ready":
            assert not ts.resource_restrictions
            assert ts in self.ready
            assert ts not in self.constrained
        else:
            assert ts.resource_restrictions
            assert ts.state == "constrained"
            assert ts not in self.ready
            assert ts in self.constrained

        assert ts.key not in self.data
        assert not ts.done
        assert not ts.waiting_for_data
        assert all(
            dep.key in self.data or dep.key in self.actors for dep in ts.dependencies
        )

    def _validate_task_waiting(self, ts: TaskState) -> None:
        assert ts.key not in self.data
        assert not ts.done
        if ts.dependencies and ts.run_spec:
            assert not all(dep.key in self.data for dep in ts.dependencies)

    def _validate_task_flight(self, ts: TaskState) -> None:
        """Validate tasks:

        - ts.state == flight
        - ts.state == cancelled, ts.previous == flight
        - ts.state == resumed, ts.previous == flight, ts.next == waiting
        """
        assert ts.key not in self.data
        assert ts in self.in_flight_tasks
        for dep in ts.dependents:
            assert dep not in self.ready
            assert dep not in self.constrained
        assert ts.coming_from
        assert ts.coming_from in self.in_flight_workers
        assert ts.key in self.in_flight_workers[ts.coming_from]

    def _validate_task_fetch(self, ts: TaskState) -> None:
        assert ts.key not in self.data
        assert self.address not in ts.who_has
        assert not ts.done
        assert ts.who_has
        for w in ts.who_has:
            assert ts.key in self.has_what[w]
            assert ts in self.data_needed[w]

    def _validate_task_missing(self, ts: TaskState) -> None:
        assert ts.key not in self.data
        assert not ts.who_has
        assert not ts.done
        assert not any(ts.key in has_what for has_what in self.has_what.values())
        assert ts in self.missing_dep_flight

    def _validate_task_cancelled(self, ts: TaskState) -> None:
        assert ts.next is None
        if ts.previous in ("executing", "long-running"):
            self._validate_task_executing(ts)
        else:
            assert ts.previous == "flight"
            self._validate_task_flight(ts)

    def _validate_task_resumed(self, ts: TaskState) -> None:
        if ts.previous in ("executing", "long-running"):
            assert ts.next == "fetch"
            self._validate_task_executing(ts)
        else:
            assert ts.previous == "flight"
            assert ts.next == "waiting"
            self._validate_task_flight(ts)

    def _validate_task_released(self, ts: TaskState) -> None:
        assert ts.key not in self.data
        assert not ts.next
        assert not ts.previous
        for tss in self.data_needed.values():
            assert ts not in tss
        assert ts not in self.executing
        assert ts not in self.in_flight_tasks
        assert ts not in self.missing_dep_flight

        # The below assert statement is true most of the time. If a task performs the
        # transition flight->cancel->waiting, its dependencies are normally in released
        # state. However, the compute-task call for their previous dependent provided
        # them with who_has, such that this assert is no longer true.
        #
        # assert not any(ts.key in has_what for has_what in self.has_what.values())

        assert not ts.waiting_for_data
        assert not ts.done
        assert not ts.exception
        assert not ts.traceback

    def validate_task(self, ts: TaskState) -> None:
        try:
            if ts.key in self.tasks:
                assert self.tasks[ts.key] is ts
            if ts.state == "memory":
                self._validate_task_memory(ts)
            elif ts.state == "waiting":
                self._validate_task_waiting(ts)
            elif ts.state == "missing":
                self._validate_task_missing(ts)
            elif ts.state == "cancelled":
                self._validate_task_cancelled(ts)
            elif ts.state == "resumed":
                self._validate_task_resumed(ts)
            elif ts.state in ("ready", "constrained"):
                self._validate_task_ready(ts)
            elif ts.state in ("executing", "long-running"):
                self._validate_task_executing(ts)
            elif ts.state == "flight":
                self._validate_task_flight(ts)
            elif ts.state == "fetch":
                self._validate_task_fetch(ts)
            elif ts.state == "released":
                self._validate_task_released(ts)
        except Exception as e:
            logger.exception(e)
            raise InvalidTaskState(
                key=ts.key, state=ts.state, story=self.story(ts)
            ) from e

    def validate_state(self) -> None:
        for ts in self.tasks.values():
            # check that worker has task
            for worker in ts.who_has:
                assert worker != self.address
                assert ts.key in self.has_what[worker]
            # check that deps have a set state and that dependency<->dependent links
            # are there
            for dep in ts.dependencies:
                # self.tasks was just a dict of tasks
                # and this check was originally that the key was in `task_state`
                # so we may have popped the key out of `self.tasks` but the
                # dependency can still be in `memory` before GC grabs it...?
                # Might need better bookkeeping
                assert self.tasks[dep.key] is dep
                assert ts in dep.dependents, ts

            for ts_wait in ts.waiting_for_data:
                assert self.tasks[ts_wait.key] is ts_wait
                assert ts_wait.state in WAITING_FOR_DATA, ts_wait

        # FIXME https://github.com/dask/distributed/issues/6319
        # assert self.waiting_for_data_count == sum(
        #     bool(ts.waiting_for_data) for ts in self.tasks.values()
        # )

        for worker, keys in self.has_what.items():
            assert worker != self.address
            for k in keys:
                assert k in self.tasks, self.story(k)
                assert worker in self.tasks[k].who_has

        # Test contents of the various sets of TaskState objects
        for worker, tss in self.data_needed.items():
            for ts in tss:
                assert ts.state == "fetch"
                assert worker in ts.who_has
        for ts in self.missing_dep_flight:
            assert ts.state == "missing"
        for ts in self.ready:
            assert ts.state == "ready"
        for ts in self.constrained:
            assert ts.state == "constrained"
        for ts in self.executing:
            assert ts.state == "executing" or (
                ts.state in ("cancelled", "resumed") and ts.previous == "executing"
            ), ts
        for ts in self.long_running:
            assert ts.state == "long-running" or (
                ts.state in ("cancelled", "resumed") and ts.previous == "long-running"
            ), ts
        for ts in self.in_flight_tasks:
            assert ts.state == "flight" or (
                ts.state in ("cancelled", "resumed") and ts.previous == "flight"
            ), ts

        # Test that there aren't multiple TaskState objects with the same key in any
        # Set[TaskState]. See note in TaskState.__hash__.
        for ts in chain(
            *self.data_needed.values(),
            self.missing_dep_flight,
            self.ready,
            self.constrained,
            self.in_flight_tasks,
            self.executing,
            self.long_running,
        ):
            assert self.tasks[ts.key] is ts

        for ts in self.tasks.values():
            self.validate_task(ts)

        if self.transition_counter_max:
            assert self.transition_counter < self.transition_counter_max

        self._validate_resources()

    def _validate_resources(self) -> None:
        """Assert that available_resources + resources held by tasks = total_resources"""
        assert self.total_resources.keys() == self.available_resources.keys()
        total = self.total_resources.copy()
        for k, v in self.available_resources.items():
            assert v > -1e-9, self.available_resources
            total[k] -= v
        for ts in self.all_running_tasks:
            for k, v in ts.resource_restrictions.items():
                assert v >= 0, (ts, ts.resource_restrictions)
                total[k] -= v

        assert all((abs(v) < 1e-9) for v in total.values()), total


class BaseWorker(abc.ABC):
    """Wrapper around the :class:`WorkerState` that implements instructions handling.
    This is an abstract class with several ``@abc.abstractmethod`` methods, to be
    subclassed by :class:`~distributed.worker.Worker` and by unit test mock-ups.
    """

    state: WorkerState
    _async_instructions: set[asyncio.Task]

    def __init__(self, state: WorkerState):
        self.state = state
        self._async_instructions = set()

    def _handle_stimulus_from_task(self, task: asyncio.Task[StateMachineEvent]) -> None:
        """An asynchronous instruction just completed; process the returned stimulus."""
        self._async_instructions.remove(task)
        try:
            # This *should* never raise any other exceptions
            stim = task.result()
        except asyncio.CancelledError:
            # This should exclusively happen in Worker.close()
            return
        self.handle_stimulus(stim)

    def handle_stimulus(self, *stims: StateMachineEvent) -> None:
        """Forward one or more external stimuli to :meth:`WorkerState.handle_stimulus`
        and process the returned instructions, invoking the relevant Worker callbacks
        (``@abc.abstractmethod`` methods below).

        Spawn asyncio tasks for all asynchronous instructions and start tracking them.

        See also
        --------
        WorkerState.handle_stimulus
        """
        instructions = self.state.handle_stimulus(*stims)

        for inst in instructions:
            task: asyncio.Task | None = None

            if isinstance(inst, SendMessageToScheduler):
                self.batched_send(inst.to_dict())

            elif isinstance(inst, GatherDep):
                assert inst.to_gather
                keys_str = ", ".join(peekn(27, inst.to_gather)[0])
                if len(keys_str) > 80:
                    keys_str = keys_str[:77] + "..."
                task = asyncio.create_task(
                    self.gather_dep(
                        inst.worker,
                        inst.to_gather,
                        total_nbytes=inst.total_nbytes,
                        stimulus_id=inst.stimulus_id,
                    ),
                    name=f"gather_dep({inst.worker}, {{{keys_str}}})",
                )

            elif isinstance(inst, Execute):
                task = asyncio.create_task(
                    self.execute(inst.key, stimulus_id=inst.stimulus_id),
                    name=f"execute({inst.key})",
                )

            elif isinstance(inst, RetryBusyWorkerLater):
                task = asyncio.create_task(
                    self.retry_busy_worker_later(inst.worker),
                    name=f"retry_busy_worker_later({inst.worker})",
                )

            else:
                raise TypeError(inst)  # pragma: nocover

            if task is not None:
                self._async_instructions.add(task)
                task.add_done_callback(self._handle_stimulus_from_task)

    async def close(self, timeout: float = 30) -> None:
        """Cancel all asynchronous instructions"""
        if not self._async_instructions:
            return
        for task in self._async_instructions:
            task.cancel()
        # async tasks can handle cancellation and could take an arbitrary amount
        # of time to terminate
        _, pending = await asyncio.wait(self._async_instructions, timeout=timeout)
        for task in pending:
            logger.error(
                f"Failed to cancel asyncio task after {timeout} seconds: {task}"
            )

    @abc.abstractmethod
    def batched_send(self, msg: dict[str, Any]) -> None:
        """Send a fire-and-forget message to the scheduler through bulk comms.

        Parameters
        ----------
        msg: dict
            msgpack-serializable message to send to the scheduler.
            Must have a 'op' key which is registered in Scheduler.stream_handlers.
        """
        ...

    @abc.abstractmethod
    async def gather_dep(
        self,
        worker: str,
        to_gather: Collection[str],
        total_nbytes: int,
        *,
        stimulus_id: str,
    ) -> StateMachineEvent:
        """Gather dependencies for a task from a worker who has them

        Parameters
        ----------
        worker : str
            Address of worker to gather dependencies from
        to_gather : list
            Keys of dependencies to gather from worker -- this is not
            necessarily equivalent to the full list of dependencies of ``dep``
            as some dependencies may already be present on this worker.
        total_nbytes : int
            Total number of bytes for all the dependencies in to_gather combined
        """
        ...

    @abc.abstractmethod
    async def execute(self, key: str, *, stimulus_id: str) -> StateMachineEvent:
        """Execute a task"""
        ...

    @abc.abstractmethod
    async def retry_busy_worker_later(self, worker: str) -> StateMachineEvent:
        """Wait some time, then take a peer worker out of busy state"""
        ...


class DeprecatedWorkerStateAttribute:
    name: str
    target: str | None

    def __init__(self, target: str | None = None):
        self.target = target

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def _warn_deprecated(self) -> None:
        warnings.warn(
            f"The `Worker.{self.name}` attribute has been moved to "
            f"`Worker.state.{self.target or self.name}`",
            FutureWarning,
        )

    def __get__(self, instance: Worker | None, owner: type[Worker]) -> Any:
        if instance is None:
            # This is triggered by Sphinx
            return None  # pragma: nocover
        self._warn_deprecated()
        return getattr(instance.state, self.target or self.name)

    def __set__(self, instance: Worker, value: Any) -> None:
        self._warn_deprecated()
        setattr(instance.state, self.target or self.name, value)
