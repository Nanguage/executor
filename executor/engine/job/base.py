import asyncio
import typing as T
from datetime import datetime
from pathlib import Path
from copy import copy
import itertools

import cloudpickle

from .utils import (
    JobStatusAttr, JobEmitError, InvalidStateError, JobStatusType,
    ExecutorError,
)
from .condition import Condition, AfterOthers, AllSatisfied
from ..middle.capture import CaptureOut
from ..middle.dir import ChDir
from ..log import logger
from ..base import ExecutorObj


if T.TYPE_CHECKING:
    from ..core import Engine


def get_callable_name(callable) -> str:
    if hasattr(callable, "func"):
        inner_func = getattr(callable, "func")
        return get_callable_name(inner_func)
    elif hasattr(callable, "__name__"):
        return getattr(callable, "__name__")
    elif hasattr(callable, "__class__"):
        return getattr(callable, "__class__").__name__
    else:  # pragma: no cover
        return str(callable)


class StopResolve(Exception):
    pass


class JobFuture():
    def __init__(self, job_id: str) -> None:
        """A future object for job."""
        self.job_id = job_id
        self._result: T.Optional[T.Any] = None
        self._exception: T.Optional[Exception] = None
        self.done_callbacks: T.List[T.Callable] = []
        self.error_callbacks: T.List[T.Callable] = []

    def result(self) -> T.Any:
        return self._result

    def set_result(self, result: T.Any) -> None:
        self._result = result

    def exception(self) -> T.Optional[Exception]:
        return self._exception

    def set_exception(self, exception: Exception) -> None:
        self._exception = exception

    def add_done_callback(self, callback: T.Callable) -> None:
        self.done_callbacks.append(callback)

    def add_error_callback(self, callback: T.Callable) -> None:
        self.error_callbacks.append(callback)


class Job(ExecutorObj):

    status = JobStatusAttr()

    def __init__(
            self,
            func: T.Callable,
            args: T.Optional[tuple] = None,
            kwargs: T.Optional[dict] = None,
            callback: T.Optional[T.Callable[[T.Any], None]] = None,
            error_callback: T.Optional[T.Callable[[Exception], None]] = None,
            retries: int = 0,
            retry_time_delta: float = 0.0,
            name: T.Optional[str] = None,
            condition: T.Optional[Condition] = None,
            wait_time_delta: float = 0.01,
            redirect_out_err: bool = False,
            change_dir: bool = False,
            **attrs
            ) -> None:
        """Base class for job.

        Args:
            func: The function to be executed.
            args: The positional arguments to be passed to the function.
            kwargs: The keyword arguments to be passed to the function.
            callback: The callback function to be called when the job is done.
            error_callback: The callback function to be called
                when the job is failed.
            retries: The number of retries when the job is failed.
            retry_time_delta: The time delta between retries.
            name: The name of the job.
            condition: The condition of the job.
            wait_time_delta: The time delta between each
                check of the condition.
            redirect_out_err: Whether to redirect the stdout
                and stderr to the log.
            change_dir: Whether to change the working directory
                to the log directory.
            **attrs: The attributes of the job.
        """
        super().__init__()
        self.future = JobFuture(self.id)
        self.func = func
        self.args = args or tuple()
        self.kwargs = kwargs or {}
        if callback is not None:
            self.future.add_done_callback(callback)
        if error_callback is not None:
            self.future.add_error_callback(error_callback)
        self.retries = retries
        self.retry_remain = retries
        self.retry_time_delta = retry_time_delta
        self.engine: T.Optional["Engine"] = None
        self._status: str = "created"
        self.name = name or func.__name__
        self.attrs = attrs
        self.task: T.Optional[asyncio.Task] = None
        self.condition = condition
        self.wait_time_delta = wait_time_delta
        self.redirect_out_err = redirect_out_err
        self.change_dir = change_dir
        self.created_time: datetime = datetime.now()
        self.submit_time: T.Optional[datetime] = None
        self.stoped_time: T.Optional[datetime] = None
        self._executor: T.Any = None
        self.dep_job_ids: T.List[str] = []

    def __repr__(self) -> str:
        func_name = get_callable_name(self.func)
        attrs = [
            f"status={self.status}",
            f"id={self.id}",
            f"func={func_name}",
        ]
        if self.condition:
            attrs.append(f" condition={repr(self.condition)}")
        if self.retry_remain > 0:
            attrs.append(f" retry_remain={self.retry_remain}")
        attr_str = " ".join(attrs)
        return f"<{self.__class__.__name__} {attr_str}>"

    def __str__(self) -> str:
        return repr(self)

    def has_resource(self) -> bool:
        """Check if the job has resource to run."""
        if self.engine is None:
            return False
        else:
            return self.engine.resource.n_job > 0

    def consume_resource(self) -> bool:
        """Consume the resource of the job."""
        if self.engine is None:
            return False
        else:
            self.engine.resource.n_job -= 1
            return True

    def release_resource(self) -> bool:
        """Release the resource of the job."""
        if self.engine is None:
            return False
        else:
            self.engine.resource.n_job += 1
            return True

    def resolve_dependencies(self) -> None:
        """Resolve args and kwargs
        and auto specify the condition."""
        dep_jobs_ids: T.List[str] = []
        args = itertools.chain(self.args, self.kwargs.values())
        for arg in args:
            if isinstance(arg, JobFuture):
                dep_jobs_ids.append(arg.job_id)
        if len(dep_jobs_ids) > 0:
            after_others = AfterOthers(dep_jobs_ids)
            if self.condition is None:
                self.condition = after_others
            else:
                self.condition = AllSatisfied([self.condition, after_others])
        self.dep_job_ids = dep_jobs_ids

    async def _resolve_arg(self, arg: T.Union[JobFuture, T.Any]) -> T.Any:
        if isinstance(arg, JobFuture):
            assert self.engine is not None
            job = self.engine.jobs.get_job_by_id(arg.job_id)
            if job.status == "done":
                return job.result()
            elif job.status == "failed":
                msg = f"Job {self} cancelled because " \
                      f"of upstream job {job} failed."
                logger.warning(msg)
                await self.cancel()
                raise StopResolve(msg)
            elif job.status == "cancelled":
                msg = f"Job {self} cancelled because " \
                      f"of upstream job {job} cancelled."
                logger.warning(msg)
                await self.cancel()
                raise StopResolve(msg)
            else:  # pragma: no cover
                raise ExecutorError("Unreachable code.")
        else:
            return arg

    async def resolve_args(self):
        """Resolve args and kwargs."""
        if len(self.dep_job_ids) > 0:
            args = []
            kwargs = {}
            for arg in self.args:
                resolved = await self._resolve_arg(arg)
                args.append(resolved)
            for key, value in self.kwargs.items():
                resolved = await self._resolve_arg(value)
                kwargs[key] = resolved
            self.args = tuple(args)
            self.kwargs = kwargs

    def runnable(self) -> bool:
        """Check if the job is runnable.
        Job is runnable if:
        1. engine is not None.
        2. condition is None and condition is satisfied.
        3. has resource."""
        if self.engine is None:
            return False
        if self.condition is not None:
            return self.condition.satisfy(self.engine) and self.has_resource()
        else:
            return self.has_resource()

    async def emit(self) -> asyncio.Task:
        """Emit the job to the engine."""
        logger.info(f"Emit job {self}, watting for run.")
        if self.status != 'pending':
            raise JobEmitError(
                f"{self} is not in valid status(pending)")
        self.resolve_dependencies()
        self.submit_time = datetime.now()
        loop = asyncio.get_running_loop()
        task = loop.create_task(self.wait_and_run())
        self.task = task
        return task

    def process_func(self):
        """Process(decorate) the target func, before run.
        For example, let the func
        change the dir, redirect the stdout and stderr
        before the actual run."""
        cache_dir = self.cache_dir.resolve()
        if self.redirect_out_err and (not isinstance(self.func, CaptureOut)):
            path_stdout = cache_dir / 'stdout.txt'
            path_stderr = cache_dir / 'stderr.txt'
            self.func = CaptureOut(self.func, path_stdout, path_stderr)
        if self.change_dir:
            self.func = ChDir(self.func, cache_dir)

    async def wait_and_run(self):
        """Wait for the condition satisfied and run the job."""
        while True:
            if self.runnable() and self.consume_resource():
                logger.info(f"Start run job {self}")
                self.process_func()
                try:
                    await self.resolve_args()
                except StopResolve:
                    break
                self.status = "running"
                try:
                    res = await self.run()
                    await self.on_done(res)
                    return res
                except Exception as e:
                    await self.on_failed(e)
                    break
            else:
                await asyncio.sleep(self.wait_time_delta)

    async def run(self):
        """Run the job."""
        pass

    async def rerun(self, check_status: bool = True):
        """Rerun the job."""
        _valid_status = ("cancelled", "done", "failed")
        if check_status and (self.status not in _valid_status):
            raise JobEmitError(
                f"{self} is not in valid status({_valid_status})")
        logger.info(f"Rerun job {self}")
        self.status = "pending"
        await self.emit()

    def _on_finish(self, new_status: JobStatusType = "done"):
        self.status = new_status
        self.release_resource()

    async def on_done(self, res):
        """Callback when the job is done."""
        logger.info(f"Job {self} done.")
        self.future.set_result(res)
        for callback in self.future.done_callbacks:
            callback(res)
        self._on_finish("done")

    async def on_failed(self, e: Exception):
        """Callback when the job is failed."""
        logger.error(f"Job {self} failed: {repr(e)}")
        assert self.engine is not None
        if self.engine.print_traceback:
            logger.exception(e)
        self.future.set_exception(e)
        for err_callback in self.future.error_callbacks:
            err_callback(e)
        if self.retry_remain > 0:
            self._on_finish("pending")
            self.retry_remain -= 1
            await asyncio.sleep(self.retry_time_delta)
            await self.rerun(check_status=False)
        else:
            self._on_finish("failed")

    async def cancel(self):
        """Cancel the job."""
        logger.info(f"Cancel job {self}.")
        self.task.cancel()
        if self.status == "running":
            try:
                self.clear_context()
            except Exception as e:  # pragma: no cover
                print(str(e))
            finally:
                self._on_finish("cancelled")
        elif self.status == "pending":
            self.status = "cancelled"

    def clear_context(self):
        """Clear the context of the job."""
        pass

    def result(self) -> T.Any:
        """Get the result of the job."""
        if self.status != "done":
            raise InvalidStateError(f"{self} is not done.")
        return self.future.result()

    def exception(self):
        """Get the exception of the job."""
        return self.future.exception()

    def serialization(self) -> bytes:
        """Serialize the job to bytes."""
        job = copy(self)
        job.task = None
        job.engine = None
        job._executor = None
        bytes_ = cloudpickle.dumps(job)
        return bytes_

    @staticmethod
    def deserialization(bytes_: bytes) -> "Job":
        """Deserialize the job from bytes."""
        job: "Job" = cloudpickle.loads(bytes_)
        return job

    async def join(self, timeout: T.Optional[float] = None):
        """Wait for the job done."""
        if self.task is None:
            raise InvalidStateError(f"{self} is not emitted.")
        await asyncio.wait([self.task], timeout=timeout)

    @property
    def cache_dir(self) -> T.Optional[Path]:
        """Get the cache dir of the job."""
        if self.engine is None:
            return None
        parent = self.engine.cache_dir
        path = parent / self.id
        path.mkdir(parents=True, exist_ok=True)
        return path
