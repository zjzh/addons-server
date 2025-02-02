"""Loads and instantiates Celery, registers our tasks, and performs any other
necessary Celery-related setup. Also provides Celery-related utility methods,
in particular exposing a shortcut to the @task decorator.

Please note that this module should not import model-related code because
Django may not be properly set-up during import time (e.g if this module
is directly being run/imported by Celery)
"""

import datetime

from django.core.cache import cache

from celery import Celery, group
from celery.signals import task_failure, task_postrun, task_prerun
from django_statsd.clients import statsd
from kombu import serialization
from post_request_task.task import PostRequestTask

import olympia.core.logger


log = olympia.core.logger.getLogger('z.task')


class AMOTask(PostRequestTask):
    """A custom celery Task base class that inherits from `PostRequestTask`
    to delay tasks and adds a special hack to still perform a serialization
    roundtrip in eager mode, to mimic what happens in production in tests.

    The serialization is applied both to apply_async() and apply() to work
    around the fact that celery groups have their own apply_async() method that
    directly calls apply() on each task in eager mode.

    Note that we should never somehow be using eager mode with actual workers,
    that would cause them to try to serialize data that has already been
    serialized...
    """

    abstract = True

    def _serialize_args_and_kwargs_for_eager_mode(
        self, args=None, kwargs=None, **options
    ):
        producer = options.get('producer')
        with app.producer_or_acquire(producer) as eager_producer:
            serializer = options.get('serializer', eager_producer.serializer)
            body = args, kwargs
            content_type, content_encoding, data = serialization.dumps(body, serializer)
            args, kwargs = serialization.loads(data, content_type, content_encoding)
        return args, kwargs

    def apply_async(self, args=None, kwargs=None, **options):
        if app.conf.task_always_eager:
            args, kwargs = self._serialize_args_and_kwargs_for_eager_mode(
                args=args, kwargs=kwargs, **options
            )

        return super().apply_async(args=args, kwargs=kwargs, **options)

    def apply(self, args=None, kwargs=None, **options):
        if app.conf.task_always_eager:
            args, kwargs = self._serialize_args_and_kwargs_for_eager_mode(
                args=args, kwargs=kwargs, **options
            )

        return super().apply(args=args, kwargs=kwargs, **options)


app = Celery('olympia', task_cls=AMOTask)
task = app.task

app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


@task_failure.connect
def process_failure_signal(
    exception, traceback, sender, task_id, signal, args, kwargs, einfo, **kw
):
    """Catch any task failure signals from within our worker processes and log
    them as exceptions, so they appear in Sentry and ordinary logging
    output."""

    exc_info = (type(exception), exception, traceback)
    log.error(
        'Celery TASK exception: {0.__name__}: {1}'.format(*exc_info),
        exc_info=exc_info,
        extra={
            'data': {
                'task_id': task_id,
                'sender': sender,
                'args': args,
                'kwargs': kwargs,
            }
        },
    )


@task_prerun.connect
def start_task_timer(task_id, task, **kw):
    timer = TaskTimer()
    log.info(
        'starting task timer; id={id}; name={name}; '
        'current_dt={current_dt}'.format(
            id=task_id, name=task.name, current_dt=timer.current_datetime
        )
    )

    # Cache start time for one hour. This will allow us to catch crazy long
    # tasks. Currently, stats indexing tasks run around 20-30 min.
    expiration = 60 * 60
    cache_key = timer.cache_key(task_id)
    cache.set(cache_key, timer.current_epoch_ms, expiration)


@task_postrun.connect
def track_task_run_time(task_id, task, **kw):
    timer = TaskTimer()
    start_time = cache.get(timer.cache_key(task_id))
    if start_time is None:
        log.info(
            'could not track task run time; id={id}; name={name}; '
            'current_dt={current_dt}'.format(
                id=task_id, name=task.name, current_dt=timer.current_datetime
            )
        )
    else:
        run_time = timer.current_epoch_ms - start_time
        log.info(
            'tracking task run time; id={id}; name={name}; '
            'run_time={run_time}; current_dt={current_dt}'.format(
                id=task_id,
                name=task.name,
                current_dt=timer.current_datetime,
                run_time=run_time,
            )
        )
        statsd.timing(f'tasks.{task.name}', run_time)
        cache.delete(timer.cache_key(task_id))


class TaskTimer:
    def __init__(self):
        from olympia.amo.utils import utc_millesecs_from_epoch

        self.current_datetime = datetime.datetime.now()
        self.current_epoch_ms = utc_millesecs_from_epoch(self.current_datetime)

    def cache_key(self, task_id):
        return f'task_start_time.{task_id}'


def create_chunked_tasks_signatures(
    task, items, chunk_size, task_args=None, task_kwargs=None
):
    """
    Splits a task depending on a list of items into a bunch of tasks of the
    specified chunk_size, passing a chunked queryset and optional additional
    arguments to each.

    Return the group of task signatures without executing it."""
    from olympia.amo.utils import chunked

    if task_args is None:
        task_args = ()
    if task_kwargs is None:
        task_kwargs = {}

    tasks = [
        task.si(chunk, *task_args, **task_kwargs)
        for chunk in chunked(items, chunk_size)
    ]
    log.info('Created a group of %s tasks for task "%s".', len(tasks), str(task.name))
    return group(tasks)
