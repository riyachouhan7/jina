import argparse
import multiprocessing
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Type, Union, Dict, Optional

from jina.serve.runtimes.asyncio import AsyncNewLoopRuntime
from jina.jaml import JAML
from jina.orchestrate.peas.helper import _get_event, _get_worker, ConditionalEvent
from jina import __stop_msg__, __ready_msg__, __windows__
from jina.enums import PeaRoleType, RuntimeBackendType
from jina.excepts import RuntimeFailToStart, RuntimeRunForeverEarlyError
from jina.helper import typename
from jina.logging.logger import JinaLogger

__all__ = ['BasePea', 'Pea']


def run(
    args: 'argparse.Namespace',
    name: str,
    runtime_cls: Type[AsyncNewLoopRuntime],
    envs: Dict[str, str],
    is_started: Union['multiprocessing.Event', 'threading.Event'],
    is_shutdown: Union['multiprocessing.Event', 'threading.Event'],
    is_ready: Union['multiprocessing.Event', 'threading.Event'],
    cancel_event: Union['multiprocessing.Event', 'threading.Event'],
    jaml_classes: Optional[Dict] = None,
):
    """Method representing the :class:`BaseRuntime` activity.

    This method is the target for the Pea's `thread` or `process`

    .. note::
        :meth:`run` is running in subprocess/thread, the exception can not be propagated to the main process.
        Hence, please do not raise any exception here.

    .. note::
        Please note that env variables are process-specific. Subprocess inherits envs from
        the main process. But Subprocess's envs do NOT affect the main process. It does NOT
        mess up user local system envs.

    .. warning::
        If you are using ``thread`` as backend, envs setting will likely be overidden by others

    .. note::
        `jaml_classes` contains all the :class:`JAMLCompatible` classes registered in the main process.
        When using `spawn` as the multiprocessing start method, passing this argument to `run` method re-imports
        & re-registers all `JAMLCompatible` classes.

    :param args: namespace args from the Pea
    :param name: name of the Pea to have proper logging
    :param runtime_cls: the runtime class to instantiate
    :param envs: a dictionary of environment variables to be set in the new Process
    :param is_started: concurrency event to communicate runtime is properly started. Used for better logging
    :param is_shutdown: concurrency event to communicate runtime is terminated
    :param is_ready: concurrency event to communicate runtime is ready to receive messages
    :param cancel_event: concurrency event to receive cancelling signal from the Pea. Needed by some runtimes
    :param jaml_classes: all the `JAMLCompatible` classes imported in main process
    """
    logger = JinaLogger(name, **vars(args))

    def _unset_envs():
        if envs and args.runtime_backend != RuntimeBackendType.THREAD:
            for k in envs.keys():
                os.environ.pop(k, None)

    def _set_envs():
        if args.env:
            if args.runtime_backend == RuntimeBackendType.THREAD:
                logger.warning(
                    'environment variables should not be set when runtime="thread".'
                )
            else:
                os.environ.update({k: str(v) for k, v in envs.items()})

    try:
        _set_envs()
        runtime = runtime_cls(
            args=args,
            cancel_event=cancel_event,
        )
    except Exception as ex:
        logger.error(
            f'{ex!r} during {runtime_cls!r} initialization'
            + f'\n add "--quiet-error" to suppress the exception details'
            if not args.quiet_error
            else '',
            exc_info=not args.quiet_error,
        )
    else:
        if not is_shutdown.is_set():
            is_started.set()
            with runtime:
                is_ready.set()
                runtime.run_forever()
    finally:
        _unset_envs()
        is_shutdown.set()
        logger.debug(f' Process terminated')


class BasePea(ABC):
    """
    :class:`BasePea` is an interface from which all the classes managing the lifetime of a Runtime inside a local process,
    container or in a remote JinaD instance (to come) must inherit.

    It exposes the required APIs so that the `BasePea` can be handled by the `cli` api as a context manager or by a `Deployment`.

    What makes a BasePea a BasePea is that it manages the lifecycle of a Runtime (gateway or not gateway)
    """

    def __init__(self, args: 'argparse.Namespace'):
        self.args = args

        if hasattr(self.args, 'port_expose'):
            self.args.port_in = self.args.port_expose
        self.args.parallel = self.args.shards
        self.name = self.args.name or self.__class__.__name__
        self.is_forked = False
        self.logger = JinaLogger(self.name, **vars(self.args))

        if self.args.runtime_backend == RuntimeBackendType.THREAD:
            self.logger.warning(
                f' Using Thread as runtime backend is not recommended for production purposes. It is '
                f'just supposed to be used for easier debugging. Besides the performance considerations, it is'
                f'specially dangerous to mix `Executors` running in different types of `RuntimeBackends`.'
            )

        self._envs = {'JINA_DEPLOYMENT_NAME': self.name}
        if self.args.quiet:
            self._envs['JINA_LOG_CONFIG'] = 'QUIET'
        if self.args.env:
            self._envs.update(self.args.env)

        # arguments needed to create `runtime` and communicate with it in the `run` in the stack of the new process
        # or thread.f
        test_worker = {
            RuntimeBackendType.THREAD: threading.Thread,
            RuntimeBackendType.PROCESS: multiprocessing.Process,
        }.get(getattr(args, 'runtime_backend', RuntimeBackendType.THREAD))()
        self.is_ready = _get_event(test_worker)
        self.is_shutdown = _get_event(test_worker)
        self.cancel_event = _get_event(test_worker)
        self.is_started = _get_event(test_worker)
        self.ready_or_shutdown = ConditionalEvent(
            getattr(args, 'runtime_backend', RuntimeBackendType.THREAD),
            events_list=[self.is_ready, self.is_shutdown],
        )
        self.daemon = self.args.daemon
        self.runtime_ctrl_address = self._get_control_address()
        self._timeout_ctrl = self.args.timeout_ctrl

    def _get_control_address(self):
        return f'{self.args.host}:{self.args.port_in}'

    def close(self) -> None:
        """Close the Pea

        This method makes sure that the `Process/thread` is properly finished and its resources properly released
        """
        self.logger.debug('waiting for ready or shutdown signal from runtime')
        if not self.is_shutdown.is_set() and self.is_started.is_set():
            try:
                self.logger.debug(f'terminate')
                self._terminate()
                if not self.is_shutdown.wait(
                    timeout=self._timeout_ctrl if not __windows__ else 1.0
                ):
                    if not __windows__:
                        raise Exception(
                            f'Shutdown signal was not received for {self._timeout_ctrl} seconds'
                        )
                    else:
                        self.logger.warning(
                            'Pea was forced to close after 1 second. Graceful closing is not available on Windows.'
                        )
            except Exception as ex:
                self.logger.error(
                    f'{ex!r} during {self.close!r}'
                    + f'\n add "--quiet-error" to suppress the exception details'
                    if not self.args.quiet_error
                    else '',
                    exc_info=not self.args.quiet_error,
                )
        else:
            # here shutdown has been set already, therefore `run` will gracefully finish
            self.logger.debug(
                f'{"shutdown is is already set" if self.is_shutdown.is_set() else "Runtime was never started"}. Runtime will end gracefully on its own'
            )
            pass
        self.is_shutdown.set()
        self.logger.debug(__stop_msg__)
        self.logger.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _wait_for_ready_or_shutdown(self, timeout: Optional[float]):
        """
        Waits for the process to be ready or to know it has failed.

        :param timeout: The time to wait before readiness or failure is determined
            .. # noqa: DAR201
        """
        return AsyncNewLoopRuntime.wait_for_ready_or_shutdown(
            timeout=timeout,
            ready_or_shutdown_event=self.ready_or_shutdown.event,
            ctrl_address=self.runtime_ctrl_address,
            timeout_ctrl=self._timeout_ctrl,
        )

    def _fail_start_timeout(self, timeout):
        """
        Closes the Pea and raises a TimeoutError with the corresponding warning messages

        :param timeout: The time to wait before readiness or failure is determined
            .. # noqa: DAR201
        """
        _timeout = timeout or -1
        self.logger.warning(
            f'{self} timeout after waiting for {self.args.timeout_ready}ms, '
            f'if your executor takes time to load, you may increase --timeout-ready'
        )
        self.close()
        raise TimeoutError(
            f'{typename(self)}:{self.name} can not be initialized after {_timeout * 1e3}ms'
        )

    def _check_failed_to_start(self):
        """
        Raises a corresponding exception if failed to start
        """
        if self.is_shutdown.is_set():
            # return too early and the shutdown is set, means something fails!!
            if not self.is_started.is_set():
                raise RuntimeFailToStart
            else:
                raise RuntimeRunForeverEarlyError

    def wait_start_success(self):
        """Block until all peas starts successfully.

        If not success, it will raise an error hoping the outer function to catch it
        """
        _timeout = self.args.timeout_ready
        if _timeout <= 0:
            _timeout = None
        else:
            _timeout /= 1e3
        if self._wait_for_ready_or_shutdown(_timeout):
            self._check_failed_to_start()
            self.logger.debug(__ready_msg__)
        else:
            self._fail_start_timeout(_timeout)

    async def async_wait_start_success(self):
        """
        Wait for the `Pea` to start successfully in a non-blocking manner
        """
        import asyncio

        _timeout = self.args.timeout_ready
        if _timeout <= 0:
            _timeout = None
        else:
            _timeout /= 1e3

        timeout_ns = 1e9 * _timeout if _timeout else None
        now = time.time_ns()
        while timeout_ns is None or time.time_ns() - now < timeout_ns:

            if self.ready_or_shutdown.event.is_set():
                self._check_failed_to_start()
                self.logger.debug(__ready_msg__)
                return
            else:
                await asyncio.sleep(0.1)

        self._fail_start_timeout(_timeout)

    @property
    def role(self) -> 'PeaRoleType':
        """Get the role of this pea in a deployment
        .. #noqa: DAR201"""
        return self.args.pea_role

    @abstractmethod
    def start(self):
        """Start the BasePea.
        This method calls :meth:`start` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.
        .. #noqa: DAR201
        """
        ...

    @abstractmethod
    def _terminate(self):
        ...

    @abstractmethod
    def join(self, *args, **kwargs):
        """Joins the BasePea. Wait for the BasePea to properly terminate

        :param args: extra positional arguments
        :param kwargs: extra keyword arguments
        """
        ...


class Pea(BasePea):
    """
    :class:`Pea` is a thread/process- container of :class:`BaseRuntime`. It leverages :class:`threading.Thread`
    or :class:`multiprocessing.Process` to manage the lifecycle of a :class:`BaseRuntime` object in a robust way.

    A :class:`Pea` must be equipped with a proper :class:`Runtime` class to work.
    """

    def __init__(self, args: 'argparse.Namespace'):
        super().__init__(args)
        self.runtime_cls = self._get_runtime_cls()
        self.worker = _get_worker(
            args=args,
            target=run,
            kwargs={
                'args': args,
                'name': self.name,
                'envs': self._envs,
                'is_started': self.is_started,
                'is_shutdown': self.is_shutdown,
                'is_ready': self.is_ready,
                # the cancel event is only necessary for threads, otherwise runtimes should create and use the asyncio event
                'cancel_event': self.cancel_event
                if getattr(args, 'runtime_backend', RuntimeBackendType.THREAD)
                == RuntimeBackendType.THREAD
                else None,
                'runtime_cls': self.runtime_cls,
                'jaml_classes': JAML.registered_classes(),
            },
            name=self.name,
        )

    def start(self):

        """Start the Pea.
        This method calls :meth:`start` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.
        .. #noqa: DAR201
        """
        self.worker.start()
        self.is_forked = multiprocessing.get_start_method().lower() == 'fork'

        if not self.args.noblock_on_start:
            self.wait_start_success()
        return self

    def join(self, *args, **kwargs):
        """Joins the Pea.
        This method calls :meth:`join` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.

        :param args: extra positional arguments to pass to join
        :param kwargs: extra keyword arguments to pass to join
        """
        self.logger.debug(f' Joining the process')
        self.worker.join(*args, **kwargs)
        self.logger.debug(f' Successfully joined the process')

    def _terminate(self):
        """Terminate the Pea.
        This method calls :meth:`terminate` in :class:`threading.Thread` or :class:`multiprocesssing.Process`.
        """
        if hasattr(self.worker, 'terminate'):
            self.logger.debug(f'terminating the runtime process')
            self.worker.terminate()
            self.logger.debug(f' runtime process properly terminated')
        else:
            self.logger.debug(f'canceling the runtime thread')
            self.cancel_event.set()
            self.logger.debug(f'runtime thread properly canceled')

    def _get_runtime_cls(self) -> AsyncNewLoopRuntime:
        from jina.orchestrate.peas.helper import update_runtime_cls
        from jina.serve.runtimes import get_runtime

        update_runtime_cls(self.args)
        return get_runtime(self.args.runtime_cls)
