import time
import typing as T
import shutil

from executor.engine.core import Engine, EngineSetting
from executor.engine.job import LocalJob, ThreadJob, ProcessJob, Job
from executor.engine.job.condition import AfterAnother, AnySatisfied


test_job_cls = [LocalJob, ThreadJob, ProcessJob]


def test_submit_job():
    n_run = 0
    def callback(res):
        nonlocal n_run
        n_run += 1
        assert res == 4

    with Engine() as engine:
        for job_cls in test_job_cls:
            job = job_cls(lambda x: x**2, (2,), callback=callback)
            engine.submit(job)

        engine.wait()

    assert n_run == 3


def test_err_callback():

    def raise_err():
        raise ValueError("test")
    
    n_run = 0
    def err_callback(e):
        nonlocal n_run
        n_run += 1
        print(e)

    with Engine() as engine:
        for job_cls in test_job_cls:
            job = job_cls(
                raise_err,
                error_callback=err_callback,
            )
            engine.submit(job)
        engine.wait()

    assert n_run == 3


def test_get_job_result():
    with Engine() as engine:
        for job_cls in test_job_cls:
            job: Job = job_cls(lambda x: x**2, (2,))
            fut = engine.submit(job)
            assert fut.result() == 4


def test_parallel():
    def sleep_add(a):
        time.sleep(3)
        return a + 1

    with Engine() as engine:
        j1 = ProcessJob(sleep_add, (1,))
        j2 = ProcessJob(sleep_add, (2,))
        t1 = time.time()
        engine.submit(j1)
        engine.submit(j2)
        engine.wait()
        t2 = time.time()
        assert (t2 - t1) < 5


def run_forever():
    while True:
        1 + 1


def test_cancel_job():
    with Engine() as engine:
        for job_cls in [ProcessJob]:
            job: Job = job_cls(run_forever)
            # cancel running
            engine.submit(job)
            engine.cancel(job)
            assert job.status == "canceled"


def test_cancel_pending():
    setting = EngineSetting(max_jobs=1)
    with Engine(setting=setting) as engine:
        # cancel pending
        job1 = ProcessJob(run_forever)
        job2 = ProcessJob(run_forever)
        engine.submit(job1)
        assert job1.status == "running"
        engine.submit(job2)
        assert job2.status == "pending"
        engine.cancel(job2)
        assert job2.status == "canceled"
        engine.cancel(job1)
        assert job1.status == "canceled"


def test_cancel_all():
    # test engine.cancel_all
    with Engine() as engine:
        for job_cls in [ProcessJob]:
            for _ in range(3):
                job: Job = job_cls(run_forever)
                engine.submit(job)
                time.sleep(0.1)
                assert job.status == "running"
        engine.cancel_all()
        for job in engine.jobs:
            assert job.status == "canceled"


def test_re_submit_job():
    with Engine() as engine:
        for job_cls in test_job_cls:
            job: Job = job_cls(lambda x: x**2, (2,))
            engine.submit(job)
            engine.wait_job(job)
            assert job.status == "done"
            engine.submit(job)
            engine.wait_job(job)
            assert job.status == "done"


def test_remove_job():

    def sleep_1s():
        time.sleep(1)

    with Engine() as engine:
        for job_cls in [ThreadJob, ProcessJob]:
            # remove running job
            job: Job = job_cls(sleep_1s)
            engine.submit(job)
            assert job in engine.jobs
            assert job.status == "running"
            engine.remove(job)
            assert job not in engine.jobs


def test_capture_stdout_stderr():
    def print_hello():
        print("hello")

    def raise_exception():
        raise ValueError("error")

    def read_hello(job: Job):
        with open(job.cache_dir / 'stdout.txt') as f:
            assert f.read() == "hello\n"

    def read_stderr(job: Job):
        with open(job.cache_dir / 'stderr.txt') as f:
            assert len(f.read()) > 0

    def on_failed(err):
        print(err)

    with Engine() as engine:
        job_cls: T.Type[Job]
        for job_cls in test_job_cls:
            job = job_cls(
                print_hello, redirect_out_err=True,
                error_callback=on_failed)
            engine.submit(job)
            engine.wait_job(job)
            assert job.status == "done"
            read_hello(job)

            job = job_cls(
                raise_exception, redirect_out_err=True,
            )
            engine.submit(job)
            engine.wait_job(job)
            assert job.status == "failed"
            read_stderr(job)


def test_repr_job():
    def print_hello():
        print("hello")

    job_cls: T.Type[Job]
    for job_cls in test_job_cls:
        job1 = job_cls(print_hello)
        str(job1)
        repr(job1)
        job2 = job_cls(print_hello, condition=AfterAnother(job_id=job1.id))
        str(job2)
        repr(job2)
        job3 = job_cls(print_hello, condition=AnySatisfied(
            conditions=[AfterAnother(job_id=job1.id), AfterAnother(job_id=job2.id)]))
        str(job3)
        repr(job3)


def test_chdir():
    def write_file():
        with open("1.txt", 'w') as f:
            f.write("111")

    def write_file2():
        with open("2.txt", 'w') as f:
            f.write("222")

    with Engine() as engine:
        job_cls: T.Type[Job]
        for job_cls in test_job_cls:
            job = job_cls(write_file, change_dir=True)
            engine.submit(job)
            engine.wait_job(job)
            with open(job.cache_dir / '1.txt') as f:
                assert f.read() == "111"
            job = job_cls(write_file2, change_dir=True)
            engine.submit(job)
            engine.wait_job(job)
            with open(job.cache_dir / '2.txt') as f:
                assert f.read() == "222"


def test_engine_get_cache_dir():
    engine = Engine()
    p = engine.get_cache_dir()
    assert p == engine.get_cache_dir()
    setting = engine.setting
    setting.cache_path = "./test_cache"
    del engine
    engine = Engine(setting=setting)
    engine.get_cache_dir()
    del engine
    shutil.rmtree(setting.cache_path)


def test_job_retry():
    def raise_exception():
        print("try")
        raise ValueError("error")
    job = ProcessJob(
        raise_exception, retries=2,
        retry_time_delta=1)
    assert job.retry_count == 0
    engine = Engine()
    with engine:
        engine.submit(job)
        time.sleep(5)
    assert job.retry_count == 2
