from datetime import timedelta

from airflow.sensors.base_sensor_operator import BaseSensorOperator
from airflow.utils import timezone
from airflow.utils.db import provide_session
from airflow.utils.decorators import apply_defaults
from airflow.utils.state import State
from sqlalchemy import func, text

from ergo.config import Config
from ergo.models import ErgoTask


class TaskRequestBatchSensor(BaseSensorOperator):
    """
    TaskRequestBatchSensor is a sensor to wait for and collect all the 'scheduled' Ergo Tasks
    and stores all the relevant ones in XCom to be later pushed by 'SqsTaskPusherOperator'.

    It has certain required conditions for tasks to have:
    - the collected tasks must have the same SQS queue URL
    - the collected tasks must be in SCHEDULED or UP_FOR_RESCHEDULE states
    - the number of tasks collected must not be more than `max_requests` (10)

    However, there's an extra set of rules to finally push these collected tasks:
    - The tasks which are waiting in SCHEDULED state for more than the configured wait threshold
    must be considered urgent tasks and should be pushed without any delay.
    - If there are no urgent tasks, then wait till last retry if the number of tasks is less than `max_requests`.
    - If at any time of poke, there are `max_requests` number of collected tasks, then push these tasks.
    - If the sensor is at last retry, then push all the collected tasks regardless if they number less than `max_requests`.
    """

    filter_ergo_task = ErgoTask.state.in_(
        [State.SCHEDULED, State.UP_FOR_RESCHEDULE])

    @apply_defaults
    def __init__(
        self,
        max_requests: int,
        xcom_tasks_key: str,
        xcom_sqs_queue_url_key: str,
        *args,
        **kwargs
    ):
        _poke_interval = kwargs.get('retry_delay', timedelta(minutes=1))
        _timeout = _poke_interval * kwargs.get('retries', 10)
        kwargs['soft_fail'] = True
        kwargs['poke_interval'] = kwargs.get(
            'poke_interval', _poke_interval.total_seconds())
        kwargs['timeout'] = kwargs.get('timeout', _timeout.total_seconds())
        super().__init__(*args, **kwargs)
        self.max_requests = max_requests
        self.xcom_tasks_key = xcom_tasks_key
        self.xcom_sqs_queue_url_key = xcom_sqs_queue_url_key
        self.urgent_task_wait_threshold = Config.queue_wait_threshold

    def poke(self, context):
        now = timezone.utcnow()
        self.log.info('Querying for %s tasks...', State.QUEUED)
        queue_url = self.choose_queue()
        if not queue_url:
            self.log.info('No task is pending to be queued!')
            return False
        result = list(self.collect_tasks(queue_url))
        urgent_task_idx = -1
        for idx, task in enumerate(result):
            if now - task.ti_execution_date > self.urgent_task_wait_threshold:
                urgent_task_idx = idx
            else:
                break

        self.log.info('Query result: %s', ", ".join(
            map(lambda res: str(res), result)))
        if len(result) < self.max_requests and urgent_task_idx == -1 and context['ti'].is_eligible_to_retry():
            return False
        elif result:
            self.log.info('Found %d tasks, with %d urgent tasks',
                          len(result), urgent_task_idx + 1)
            self.xcom_push(context, self.xcom_tasks_key, result)
            self.xcom_push(context, self.xcom_sqs_queue_url_key, queue_url)
            return True
        else:
            return False

    @provide_session
    def choose_queue(self, session=None):
        wait_second = self.urgent_task_wait_threshold.total_seconds()
        col_count_group = func.count(ErgoTask.queue_url)
        col_min_execution_date = func.min(ErgoTask.ti_execution_date)
        dialect = session.bind.dialect.name
        self.log.debug(f'using dialect: {dialect}')
        if dialect == 'postgresql':
            col_is_urgent_task = (
                (func.EXTRACT(text('EPOCH'), func.now()) -
                 func.EXTRACT(text('EPOCH'), col_min_execution_date))
                >= wait_second
            )
        else:
            col_is_urgent_task = func.TIMESTAMPDIFF(
                text('SECOND'), col_min_execution_date, func.now()) >= wait_second
        valid_queues = (
            session.query(
                ErgoTask.queue_url, col_count_group, col_is_urgent_task
            ).filter(self.filter_ergo_task).group_by(ErgoTask.queue_url)
            .order_by(col_is_urgent_task.desc(), col_count_group.desc())
        )
        queue = valid_queues.first()
        return queue.queue_url if queue else None

    @provide_session
    def collect_tasks(self, queue_url, session=None):
        return (
            session.query(ErgoTask)
            .filter(self.filter_ergo_task, ErgoTask.queue_url == queue_url)
            .order_by(ErgoTask.ti_execution_date)
        ).limit(self.max_requests)
