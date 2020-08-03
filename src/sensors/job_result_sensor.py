from airflow.sensors.base_sensor_operator import BaseSensorOperator
from airflow.utils.db import provide_session
from airflow.utils.decorators import apply_defaults
from airflow.utils.state import State
from sqlalchemy.orm import joinedload

from ergo.exceptions import ErgoFailedResultException
from ergo.models import ErgoJob, ErgoTask


class ErgoJobResultSensor(BaseSensorOperator):
    @apply_defaults
    def __init__(
        self,
        pusher_task_id: str,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.pusher_task_id = pusher_task_id

    @provide_session
    def _get_ergo_task(self, ti, session=None):
        return (
            session.query(ErgoTask)
            .options(joinedload('job'))
            .filter_by(ti_task_id=self.pusher_task_id, ti_dag_id=ti.dag_id, ti_execution_date=ti.execution_date)
        ).one()

    def poke(self, context):
        ti = context['ti']
        task = self._get_ergo_task(ti)
        self.log.info('Received task - %s... STATE: %s', str(task), task.state)
        job = task.job
        if job is None or task.state in State.unfinished():
            return False
        self.log.info('Job - (%s) executed at %s',
                      str(job), str(job.response_at))
        if task.state == State.FAILED:
            raise ErgoFailedResultException(job.result_code, job.error_msg)
        return True
