from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import tempfile
import threading
import uuid
import warnings
import weakref
from collections.abc import Collection
from contextlib import suppress
from inspect import isawaitable
from queue import Empty
from time import sleep as sync_sleep
from typing import TYPE_CHECKING, ClassVar

from toolz import merge
from tornado import gen
from tornado.ioloop import IOLoop

import dask
from dask.system import CPU_COUNT
from dask.utils import parse_timedelta

from distributed import preloading
from distributed.comm import get_address_host
from distributed.comm.addressing import address_from_user_args
from distributed.core import (
    AsyncTaskGroupClosedError,
    CommClosedError,
    RPCClosed,
    Status,
    coerce_to_address,
    error_message,
)
from distributed.diagnostics.plugin import _get_plugin_name
from distributed.metrics import time
from distributed.node import ServerNode
from distributed.process import AsyncProcess
from distributed.proctitle import enable_proctitle_on_children
from distributed.protocol import pickle
from distributed.security import Security
from distributed.utils import (
    TimeoutError,
    get_ip,
    get_mp_context,
    json_load_robust,
    log_errors,
    parse_ports,
    silence_logging,
)
from distributed.worker import Worker, run
from distributed.worker_memory import (
    DeprecatedMemoryManagerAttribute,
    DeprecatedMemoryMonitor,
    NannyMemoryManager,
)

if TYPE_CHECKING:
    from distributed.diagnostics.plugin import NannyPlugin

logger = logging.getLogger(__name__)


class Nanny(ServerNode):
    """A process to manage worker processes

    The nanny spins up Worker processes, watches them, and kills or restarts
    them as necessary. It is necessary if you want to use the
    ``Client.restart`` method, or to restart the worker automatically if
    it gets to the terminate fraction of its memory limit.

    The parameters for the Nanny are mostly the same as those for the Worker
    with exceptions listed below.

    Parameters
    ----------
    env: dict, optional
        Environment variables set at time of Nanny initialization will be
        ensured to be set in the Worker process as well. This argument allows to
        overwrite or otherwise set environment variables for the Worker. It is
        also possible to set environment variables using the option
        ``distributed.nanny.environ``. Precedence as follows

            1. Nanny arguments
            2. Existing environment variables
            3. Dask configuration

        .. note::
           Some environment variables, like ``OMP_NUM_THREADS``, must be set before
           importing numpy to have effect. Others, like ``MALLOC_TRIM_THRESHOLD_`` (see
           :ref:`memtrim`), must be set before starting the Linux process. Such
           variables would be ineffective if set here or in
           ``distributed.nanny.environ``; they must be set in
           ``distributed.nanny.pre-spawn-environ`` so that they are set before spawning
           the subprocess, even if this means poisoning the process running the Nanny.

           For the same reason, be warned that changing
           ``distributed.worker.multiprocessing-method`` from ``spawn`` to ``fork`` or
           ``forkserver`` may inhibit some environment variables; if you do, you should
           set the variables yourself in the shell before you start ``dask-worker``.

    See Also
    --------
    Worker
    """

    _instances: ClassVar[weakref.WeakSet[Nanny]] = weakref.WeakSet()
    process = None
    memory_manager: NannyMemoryManager

    env: dict[str, str]
    pre_spawn_env: dict[str, str]

    # Inputs to parse_ports()
    _given_worker_port: int | str | Collection[int] | None
    _start_port: int | str | Collection[int] | None

    def __init__(  # type: ignore[no-untyped-def]
        self,
        scheduler_ip=None,
        scheduler_port=None,
        scheduler_file=None,
        worker_port: int | str | Collection[int] | None = 0,
        nthreads=None,
        loop=None,
        local_directory=None,
        services=None,
        name=None,
        memory_limit="auto",
        reconnect=True,
        validate=False,
        quiet=False,
        resources=None,
        silence_logs=None,
        death_timeout=None,
        preload=None,
        preload_argv=None,
        preload_nanny=None,
        preload_nanny_argv=None,
        security=None,
        contact_address=None,
        listen_address=None,
        worker_class=None,
        env=None,
        interface=None,
        host=None,
        port: int | str | Collection[int] | None = None,
        protocol=None,
        config=None,
        **worker_kwargs,
    ):
        if loop is not None:
            warnings.warn(
                "the `loop` kwarg to `Nanny` is ignored, and will be removed in a future release. "
                "The Nanny always binds to the current loop.",
                DeprecationWarning,
                stacklevel=2,
            )

        self._setup_logging(logger)
        self.loop = self.io_loop = IOLoop.current()

        if isinstance(security, dict):
            security = Security(**security)
        self.security = security or Security()
        assert isinstance(self.security, Security)
        self.connection_args = self.security.get_connection_args("worker")

        if local_directory is None:
            local_directory = (
                dask.config.get("temporary-directory") or tempfile.gettempdir()
            )
            self._original_local_dir = local_directory
            local_directory = os.path.join(local_directory, "dask-worker-space")
        else:
            self._original_local_dir = local_directory

        self.local_directory = local_directory
        if not os.path.exists(self.local_directory):
            os.makedirs(self.local_directory, exist_ok=True)

        self.preload = preload
        if self.preload is None:
            self.preload = dask.config.get("distributed.worker.preload")
        self.preload_argv = preload_argv
        if self.preload_argv is None:
            self.preload_argv = dask.config.get("distributed.worker.preload-argv")

        if preload_nanny is None:
            preload_nanny = dask.config.get("distributed.nanny.preload")
        if preload_nanny_argv is None:
            preload_nanny_argv = dask.config.get("distributed.nanny.preload-argv")

        self.preloads = preloading.process_preloads(
            self, preload_nanny, preload_nanny_argv, file_dir=self.local_directory
        )

        if scheduler_file:
            cfg = json_load_robust(scheduler_file)
            self.scheduler_addr = cfg["address"]
        elif scheduler_ip is None and dask.config.get("scheduler-address"):
            self.scheduler_addr = dask.config.get("scheduler-address")
        elif scheduler_port is None:
            self.scheduler_addr = coerce_to_address(scheduler_ip)
        else:
            self.scheduler_addr = coerce_to_address((scheduler_ip, scheduler_port))

        if protocol is None:
            protocol_address = self.scheduler_addr.split("://")
            if len(protocol_address) == 2:
                protocol = protocol_address[0]

        self._given_worker_port = worker_port
        self.nthreads = nthreads or CPU_COUNT
        self.reconnect = reconnect
        self.validate = validate
        self.resources = resources
        self.death_timeout = parse_timedelta(death_timeout)

        self.Worker = Worker if worker_class is None else worker_class

        self.pre_spawn_env = _get_env_variables("distributed.nanny.pre-spawn-environ")
        self.env = merge(
            self.pre_spawn_env,
            _get_env_variables("distributed.nanny.environ"),
            {k: str(v) for k, v in env.items()} if env else {},
        )

        self.config = config or dask.config.config
        worker_kwargs.update(
            {
                "port": worker_port,
                "interface": interface,
                "protocol": protocol,
                "host": host,
            }
        )
        self.worker_kwargs = worker_kwargs

        self.contact_address = contact_address

        self.services = services
        self.name = name
        self.quiet = quiet

        if silence_logs:
            silence_logging(level=silence_logs)
        self.silence_logs = silence_logs

        handlers = {
            "instantiate": self.instantiate,
            "kill": self.kill,
            "restart": self.restart,
            # cannot call it 'close' on the rpc side for naming conflict
            "get_logs": self.get_logs,
            "terminate": self.close,
            "close_gracefully": self.close_gracefully,
            "run": self.run,
            "plugin_add": self.plugin_add,
            "plugin_remove": self.plugin_remove,
        }

        self.plugins: dict[str, NannyPlugin] = {}

        super().__init__(handlers=handlers, connection_args=self.connection_args)

        self.scheduler = self.rpc(self.scheduler_addr)
        self.memory_manager = NannyMemoryManager(self, memory_limit=memory_limit)

        if (
            not host
            and not interface
            and not self.scheduler_addr.startswith("inproc://")
        ):
            host = get_ip(get_address_host(self.scheduler.address))

        self._start_port = port
        self._start_host = host
        self._interface = interface
        self._protocol = protocol

        self._listen_address = listen_address
        Nanny._instances.add(self)

    # Deprecated attributes; use Nanny.memory_manager.<name> instead
    memory_limit = DeprecatedMemoryManagerAttribute()
    memory_terminate_fraction = DeprecatedMemoryManagerAttribute()
    memory_monitor = DeprecatedMemoryMonitor()

    def __repr__(self):
        return "<Nanny: %s, threads: %d>" % (self.worker_address, self.nthreads)

    async def _unregister(self, timeout=10):
        if self.process is None:
            return
        worker_address = self.process.worker_address
        if worker_address is None:
            return

        allowed_errors = (TimeoutError, CommClosedError, EnvironmentError, RPCClosed)
        with suppress(allowed_errors):
            await asyncio.wait_for(
                self.scheduler.unregister(
                    address=self.worker_address, stimulus_id=f"nanny-close-{time()}"
                ),
                timeout,
            )

    @property
    def worker_address(self):
        return None if self.process is None else self.process.worker_address

    @property
    def worker_dir(self):
        return None if self.process is None else self.process.worker_dir

    async def start_unsafe(self):
        """Start nanny, start local process, start watching"""

        await super().start_unsafe()

        ports = parse_ports(self._start_port)
        for port in ports:
            start_address = address_from_user_args(
                host=self._start_host,
                port=port,
                interface=self._interface,
                protocol=self._protocol,
                security=self.security,
            )
            try:
                await self.listen(
                    start_address, **self.security.get_listen_args("worker")
                )
            except OSError as e:
                if len(ports) > 1 and e.errno == errno.EADDRINUSE:
                    continue
                else:
                    raise
            else:
                self._start_address = start_address
                break
        else:
            raise ValueError(
                f"Could not start Nanny on host {self._start_host} "
                f"with port {self._start_port}"
            )

        self.ip = get_address_host(self.address)

        for preload in self.preloads:
            await preload.start()

        msg = await self.scheduler.register_nanny()
        for name, plugin in msg["nanny-plugins"].items():
            await self.plugin_add(plugin=plugin, name=name)

        logger.info("        Start Nanny at: %r", self.address)
        response = await self.instantiate()

        if response != Status.running:
            await self.close()
            return

        assert self.worker_address

        self.start_periodic_callbacks()

        return self

    async def kill(self, timeout=2):
        """Kill the local worker process

        Blocks until both the process is down and the scheduler is properly
        informed
        """
        if self.process is None:
            return

        deadline = time() + timeout
        await self.process.kill(timeout=0.8 * (deadline - time()))

    async def instantiate(self) -> Status:
        """Start a local worker process

        Blocks until the process is up and the scheduler is properly informed
        """
        if self.process is None:
            worker_kwargs = dict(
                scheduler_ip=self.scheduler_addr,
                nthreads=self.nthreads,
                local_directory=self._original_local_dir,
                services=self.services,
                nanny=self.address,
                name=self.name,
                memory_limit=self.memory_manager.memory_limit,
                resources=self.resources,
                validate=self.validate,
                silence_logs=self.silence_logs,
                death_timeout=self.death_timeout,
                preload=self.preload,
                preload_argv=self.preload_argv,
                security=self.security,
                contact_address=self.contact_address,
            )
            worker_kwargs.update(self.worker_kwargs)
            self.process = WorkerProcess(
                worker_kwargs=worker_kwargs,
                silence_logs=self.silence_logs,
                on_exit=self._on_worker_exit_sync,
                worker=self.Worker,
                env=self.env,
                pre_spawn_env=self.pre_spawn_env,
                config=self.config,
            )

        if self.death_timeout:
            try:
                result = await asyncio.wait_for(
                    self.process.start(), self.death_timeout
                )
            except TimeoutError:
                logger.error(
                    "Timed out connecting Nanny '%s' to scheduler '%s'",
                    self,
                    self.scheduler_addr,
                )
                await self.close(timeout=self.death_timeout)
                raise

        else:
            try:
                result = await self.process.start()
            except Exception:
                logger.error("Failed to start process", exc_info=True)
                await self.close()
                raise
        return result

    @log_errors
    async def plugin_add(self, plugin=None, name=None):
        if isinstance(plugin, bytes):
            plugin = pickle.loads(plugin)

        if name is None:
            name = _get_plugin_name(plugin)

        assert name

        self.plugins[name] = plugin

        logger.info("Starting Nanny plugin %s" % name)
        if hasattr(plugin, "setup"):
            try:
                result = plugin.setup(nanny=self)
                if isawaitable(result):
                    result = await result
            except Exception as e:
                msg = error_message(e)
                return msg
        if getattr(plugin, "restart", False):
            await self.restart()

        return {"status": "OK"}

    @log_errors
    async def plugin_remove(self, name=None):
        logger.info(f"Removing Nanny plugin {name}")
        try:
            plugin = self.plugins.pop(name)
            if hasattr(plugin, "teardown"):
                result = plugin.teardown(nanny=self)
                if isawaitable(result):
                    result = await result
        except Exception as e:
            msg = error_message(e)
            return msg

        return {"status": "OK"}

    async def restart(self, timeout=30):
        async def _():
            if self.process is not None:
                await self.kill()
                await self.instantiate()

        try:
            await asyncio.wait_for(_(), timeout)
        except TimeoutError:
            logger.error(
                f"Restart timed out after {timeout}s; returning before finished"
            )
            return "timed out"
        else:
            return "OK"

    def is_alive(self):
        return self.process is not None and self.process.is_alive()

    def run(self, comm, *args, **kwargs):
        return run(self, comm, *args, **kwargs)

    def _on_worker_exit_sync(self, exitcode):
        try:
            self._ongoing_background_tasks.call_soon(self._on_worker_exit, exitcode)
        except AsyncTaskGroupClosedError:  # Async task group has already been closed, so the nanny is already clos(ed|ing).
            pass

    @log_errors
    async def _on_worker_exit(self, exitcode):
        if self.status not in (
            Status.init,
            Status.closing,
            Status.closed,
            Status.closing_gracefully,
            Status.failed,
        ):
            try:
                await self._unregister()
            except OSError:
                logger.exception("Failed to unregister")
                if not self.reconnect:
                    await self.close()
                    return

        try:
            if self.status not in (
                Status.closing,
                Status.closed,
                Status.closing_gracefully,
                Status.failed,
            ):
                logger.warning("Restarting worker")
                await self.instantiate()
            elif self.status == Status.closing_gracefully:
                await self.close()

        except Exception:
            logger.error(
                "Failed to restart worker after its process exited", exc_info=True
            )

    @property
    def pid(self):
        return self.process and self.process.pid

    def _close(self, *args, **kwargs):
        warnings.warn("Worker._close has moved to Worker.close", stacklevel=2)
        return self.close(*args, **kwargs)

    def close_gracefully(self):
        """
        A signal that we shouldn't try to restart workers if they go away

        This is used as part of the cluster shutdown process.
        """
        self.status = Status.closing_gracefully

    async def close(self, timeout=5):
        """
        Close the worker process, stop all comms.
        """
        if self.status == Status.closing:
            await self.finished()
            assert self.status == Status.closed

        if self.status == Status.closed:
            return "OK"

        self.status = Status.closing
        logger.info(
            "Closing Nanny at %r.",
            self.address_safe,
        )

        for preload in self.preloads:
            await preload.teardown()

        teardowns = [
            plugin.teardown(self)
            for plugin in self.plugins.values()
            if hasattr(plugin, "teardown")
        ]

        await asyncio.gather(*(td for td in teardowns if isawaitable(td)))

        self.stop()
        try:
            if self.process is not None:
                await self.kill(timeout=timeout)
        except Exception:
            logger.exception("Error in Nanny killing Worker subprocess")
        self.process = None
        await self.rpc.close()
        self.status = Status.closed
        await super().close()
        return "OK"

    async def _log_event(self, topic, msg):
        await self.scheduler.log_event(
            topic=topic,
            msg=msg,
        )

    def log_event(self, topic, msg):
        self._ongoing_background_tasks.call_soon(self._log_event, topic, msg)


class WorkerProcess:
    running: asyncio.Event
    stopped: asyncio.Event

    env: dict[str, str]
    pre_spawn_env: dict[str, str]

    # The interval how often to check the msg queue for init
    _init_msg_interval = 0.05

    def __init__(
        self,
        worker_kwargs,
        silence_logs,
        on_exit,
        worker,
        env,
        pre_spawn_env,
        config,
    ):
        self.status = Status.init
        self.silence_logs = silence_logs
        self.worker_kwargs = worker_kwargs
        self.on_exit = on_exit
        self.process = None
        self.Worker = worker
        self.env = env
        self.pre_spawn_env = pre_spawn_env
        self.config = config.copy()

        # Ensure default clients don't propagate to subprocesses
        try:
            from distributed.client import default_client

            default_client()
            self.config.pop("scheduler", None)
            self.config.pop("shuffle", None)
        except ValueError:
            pass

        # Initialized when worker is ready
        self.worker_dir = None
        self.worker_address = None

    async def start(self) -> Status:
        """
        Ensure the worker process is started.
        """
        enable_proctitle_on_children()
        if self.status == Status.running:
            return self.status
        if self.status == Status.starting:
            await self.running.wait()
            return self.status

        self.init_result_q = init_q = get_mp_context().Queue()
        self.child_stop_q = get_mp_context().Queue()
        uid = uuid.uuid4().hex

        self.process = AsyncProcess(
            target=self._run,
            name="Dask Worker process (from Nanny)",
            kwargs=dict(
                worker_kwargs=self.worker_kwargs,
                silence_logs=self.silence_logs,
                init_result_q=self.init_result_q,
                child_stop_q=self.child_stop_q,
                uid=uid,
                Worker=self.Worker,
                env=self.env,
                config=self.config,
            ),
        )
        self.process.daemon = dask.config.get("distributed.worker.daemon", default=True)
        self.process.set_exit_callback(self._on_exit)
        self.running = asyncio.Event()
        self.stopped = asyncio.Event()
        self.status = Status.starting

        # Set selected environment variables before spawning the subprocess.
        # See note in Nanny docstring.
        os.environ.update(self.pre_spawn_env)

        try:
            await self.process.start()
        except OSError:
            logger.exception("Nanny failed to start process", exc_info=True)
            # NOTE: doesn't wait for process to terminate, just for terminate signal to be sent
            await self.process.terminate()
            self.status = Status.failed
        try:
            msg = await self._wait_until_connected(uid)
        except Exception:
            # NOTE: doesn't wait for process to terminate, just for terminate signal to be sent
            await self.process.terminate()
            self.status = Status.failed
            raise
        if not msg:
            return self.status
        self.worker_address = msg["address"]
        self.worker_dir = msg["dir"]
        assert self.worker_address
        self.status = Status.running
        self.running.set()

        init_q.close()

        return self.status

    def _on_exit(self, proc):
        if proc is not self.process:
            # Ignore exit of old process instance
            return
        self.mark_stopped()

    def _death_message(self, pid, exitcode):
        assert exitcode is not None
        if exitcode == 255:
            return "Worker process %d was killed by unknown signal" % (pid,)
        elif exitcode >= 0:
            return "Worker process %d exited with status %d" % (pid, exitcode)
        else:
            return "Worker process %d was killed by signal %d" % (pid, -exitcode)

    def is_alive(self):
        return self.process is not None and self.process.is_alive()

    @property
    def pid(self):
        return self.process.pid if self.process and self.process.is_alive() else None

    def mark_stopped(self):
        if self.status != Status.stopped:
            r = self.process.exitcode
            assert r is not None
            if r != 0:
                msg = self._death_message(self.process.pid, r)
                logger.info(msg)
            self.status = Status.stopped
            self.stopped.set()
            # Release resources
            self.process.close()
            self.init_result_q = None
            self.child_stop_q = None
            self.process = None
            # Best effort to clean up worker directory
            if self.worker_dir and os.path.exists(self.worker_dir):
                shutil.rmtree(self.worker_dir, ignore_errors=True)
            self.worker_dir = None
            # User hook
            if self.on_exit is not None:
                self.on_exit(r)

    async def kill(self, timeout: float = 2, executor_wait: bool = True) -> None:
        """
        Ensure the worker process is stopped, waiting at most
        ``timeout * 0.8`` seconds before killing it abruptly.

        When `kill` returns, the worker process has been joined.

        If the worker process does not terminate within ``timeout`` seconds,
        even after being killed, `asyncio.TimeoutError` is raised.
        """
        deadline = time() + timeout

        if self.status == Status.stopped:
            return
        if self.status == Status.stopping:
            await self.stopped.wait()
            return
        assert self.status in (
            Status.starting,
            Status.running,
            Status.failed,  # process failed to start, but hasn't been joined yet
        ), self.status
        self.status = Status.stopping
        logger.info("Nanny asking worker to close")

        process = self.process
        assert process
        queue = self.child_stop_q
        assert queue
        wait_timeout = timeout * 0.8
        queue.put(
            {
                "op": "stop",
                "timeout": wait_timeout,
                "executor_wait": executor_wait,
            }
        )
        await asyncio.sleep(0)  # otherwise we get broken pipe errors
        queue.close()
        del queue

        try:
            try:
                await process.join(wait_timeout)
                return
            except asyncio.TimeoutError:
                pass

            logger.warning(
                f"Worker process still alive after {wait_timeout} seconds, killing"
            )
            await process.kill()
            await process.join(max(0, deadline - time()))
        except ValueError as e:
            if "invalid operation on closed AsyncProcess" in str(e):
                return
            raise

    async def _wait_until_connected(self, uid):
        while True:
            if self.status != Status.starting:
                return
            # This is a multiprocessing queue and we'd block the event loop if
            # we simply called get
            try:
                msg = self.init_result_q.get_nowait()
            except Empty:
                await asyncio.sleep(self._init_msg_interval)
                continue

            if msg["uid"] != uid:  # ensure that we didn't cross queues
                continue

            if "exception" in msg:
                raise msg["exception"]
            else:
                return msg

    @classmethod
    def _run(
        cls,
        worker_kwargs,
        silence_logs,
        init_result_q,
        child_stop_q,
        uid,
        env,
        config,
        Worker,
    ):  # pragma: no cover
        try:
            os.environ.update(env)
            dask.config.refresh()
            dask.config.set(config)

            from dask.multiprocessing import default_initializer

            default_initializer()

            if silence_logs:
                logger.setLevel(silence_logs)

            IOLoop.clear_instance()
            loop = IOLoop()
            loop.make_current()
            worker = Worker(**worker_kwargs)

            async def do_stop(timeout=5, executor_wait=True):
                try:
                    await worker.close(
                        nanny=False,
                        executor_wait=executor_wait,
                        timeout=timeout,
                    )
                finally:
                    loop.stop()

            def watch_stop_q():
                """
                Wait for an incoming stop message and then stop the
                worker cleanly.
                """
                try:
                    msg = child_stop_q.get()
                except (TypeError, OSError, EOFError):
                    logger.error("Worker process died unexpectedly")
                    msg = {"op": "stop"}
                finally:
                    child_stop_q.close()
                    assert msg["op"] == "stop", msg
                    del msg["op"]
                    loop.add_callback(do_stop, **msg)

            thread = threading.Thread(
                target=watch_stop_q, name="Nanny stop queue watch"
            )
            thread.daemon = True
            thread.start()

            async def run():
                """
                Try to start worker and inform parent of outcome.
                """
                try:
                    await worker
                except Exception as e:
                    logger.exception("Failed to start worker")
                    init_result_q.put({"uid": uid, "exception": e})
                    init_result_q.close()
                    # If we hit an exception here we need to wait for a least
                    # one interval for the outside to pick up this message.
                    # Otherwise we arrive in a race condition where the process
                    # cleanup wipes the queue before the exception can be
                    # properly handled. See also
                    # WorkerProcess._wait_until_connected (the 2 is for good
                    # measure)
                    sync_sleep(cls._init_msg_interval * 2)
                else:
                    try:
                        assert worker.address
                    except ValueError:
                        pass
                    else:
                        init_result_q.put(
                            {
                                "address": worker.address,
                                "dir": worker.local_directory,
                                "uid": uid,
                            }
                        )
                        init_result_q.close()
                        await worker.finished()
                        logger.info("Worker closed")

        except Exception as e:
            logger.exception("Failed to initialize Worker")
            init_result_q.put({"uid": uid, "exception": e})
            init_result_q.close()
            # If we hit an exception here we need to wait for a least one
            # interval for the outside to pick up this message. Otherwise we
            # arrive in a race condition where the process cleanup wipes the
            # queue before the exception can be properly handled. See also
            # WorkerProcess._wait_until_connected (the 2 is for good measure)
            sync_sleep(cls._init_msg_interval * 2)
        else:
            try:
                loop.run_sync(run)
            except (TimeoutError, gen.TimeoutError):
                # Loop was stopped before wait_until_closed() returned, ignore
                pass
            except KeyboardInterrupt:
                # At this point the loop is not running thus we have to run
                # do_stop() explicitly.
                loop.run_sync(do_stop)
            finally:
                with suppress(ValueError):
                    child_stop_q.put({"op": "stop"})  # usually redundant
                with suppress(ValueError):
                    child_stop_q.close()  # usually redundant
                child_stop_q.join_thread()
                thread.join(timeout=2)


def _get_env_variables(config_key: str) -> dict[str, str]:
    cfg = dask.config.get(config_key)
    if not isinstance(cfg, dict):
        raise TypeError(  # pragma: nocover
            f"{config_key} configuration must be of type dict. Instead got {type(cfg)}"
        )
    # Override dask config with explicitly defined env variables from the OS
    cfg = {k: os.environ.get(k, str(v)) for k, v in cfg.items()}
    return cfg
