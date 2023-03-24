import functools

from dask.distributed import Client, LocalCluster

from .base import Job
from ..utils import PortManager


def get_default_client() -> Client:
    free_port = PortManager.find_free_port()
    cluster = LocalCluster(
        dashboard_address=f":{free_port}",
        asynchronous=True,
    )
    return Client(
        cluster,
        asynchronous=True,
    )


class DaskJob(Job):
    """Job that runs with Dask."""

    def has_resource(self) -> bool:
        """Check if the job has enough resource to run."""
        if self.engine is None:
            return False
        else:
            return (
                super().has_resource() and
                (self.engine.resource.n_dask > 0)
            )

    def consume_resource(self) -> bool:
        """Consume resource for the job."""
        if self.engine is None:
            return False
        else:
            self.engine.resource.n_dask -= 1
            return (
                super().consume_resource() and
                True
            )

    def release_resource(self) -> bool:
        """Release resource for the job."""
        if self.engine is None:
            return False
        else:
            self.engine.resource.n_dask += 1
            return (
                super().release_resource() and
                True
            )

    async def run(self):
        """Run job with Dask."""
        client = self.engine.dask_client
        func = functools.partial(self.func, **self.kwargs)
        fut = client.submit(func, *self.args)
        self._executor = fut
        result = await fut
        return result

    async def cancel(self):
        """Cancel job."""""
        if self.status == "running":
            await self._executor.cancel()
        await super().cancel()

    def clear_context(self):
        """Clear context."""
        self._executor = None
