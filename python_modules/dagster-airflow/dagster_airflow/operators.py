'''The dagster-airflow operators.'''
import ast
import json
import os
import sys

from abc import ABCMeta, abstractmethod
from contextlib import contextmanager

from six import string_types, with_metaclass

from airflow.exceptions import AirflowException
from airflow.operators.docker_operator import DockerOperator
from airflow.operators.python_operator import PythonOperator
from airflow.utils.file import TemporaryDirectory
from docker import APIClient, from_env

from dagster import seven
from dagster.seven.json import JSONDecodeError

from .format import format_config_for_graphql
from .query import DAGSTER_OPERATOR_COMMAND_TEMPLATE, QUERY_TEMPLATE


DOCKER_TEMPDIR = '/tmp'

DEFAULT_ENVIRONMENT = {
    'AWS_ACCESS_KEY_ID': os.getenv('AWS_ACCESS_KEY_ID'),
    'AWS_SECRET_ACCESS_KEY': os.getenv('AWS_SECRET_ACCESS_KEY'),
}

LINE_LENGTH = 100


def parse_raw_res(raw_res):
    res = None
    # FIXME
    # Unfortunately, log lines don't necessarily come back in order...
    # This is error-prone, if something else logs JSON
    lines = list(reversed(raw_res.decode('utf-8').split('\n')))
    last_line = lines[0]

    for line in lines:
        try:
            res = json.loads(line)
            break
        # If we don't get a GraphQL response, check the next line
        except JSONDecodeError:
            continue

    return (res, last_line)


def handle_errors(res, last_line):
    if res is None:
        raise AirflowException('Unhandled error type. Response: {}'.format(last_line))

    if res.get('errors'):
        raise AirflowException('Internal error in GraphQL request. Response: {}'.format(res))

    if not res.get('data', {}).get('executePlan', {}).get('__typename'):
        raise AirflowException('Unexpected response type. Response: {}'.format(res))


class DagsterOperator(with_metaclass(ABCMeta)):  # pylint:disable=no-init
    '''Abstract base class for Dagster operators.

    Implement operator_for_solid to support dynamic generation of Airflow operators corresponding to
    the execution plan steps generated by a Dagster solid.
    '''

    @classmethod
    @abstractmethod
    def operator_for_solid(
        cls, pipeline, env_config, solid_name, step_keys, dag, dag_id, op_kwargs
    ):
        pass


# pylint: disable=len-as-condition
class ModifiedDockerOperator(DockerOperator):
    """ModifiedDockerOperator supports host temporary directories on OSX.

    Incorporates https://github.com/apache/airflow/pull/4315/ and an implementation of
    https://issues.apache.org/jira/browse/AIRFLOW-3825.

    :param host_tmp_dir: Specify the location of the temporary directory on the host which will
        be mapped to tmp_dir. If not provided defaults to using the standard system temp directory.
    :type host_tmp_dir: str
    """

    def __init__(self, host_tmp_dir='/tmp', **kwargs):
        self.host_tmp_dir = host_tmp_dir
        kwargs['xcom_push'] = True
        super(ModifiedDockerOperator, self).__init__(**kwargs)

    @contextmanager
    def get_host_tmp_dir(self):
        '''Abstracts the tempdir context manager so that this can be overridden.'''
        with TemporaryDirectory(prefix='airflowtmp', dir=self.host_tmp_dir) as tmp_dir:
            yield tmp_dir

    def execute(self, context):
        '''Modified only to use the get_host_tmp_dir helper.'''
        self.log.info('Starting docker container from image %s', self.image)

        tls_config = self.__get_tls_config()

        if self.docker_conn_id:
            self.cli = self.get_hook().get_conn()
        else:
            self.cli = APIClient(base_url=self.docker_url, version=self.api_version, tls=tls_config)

        if self.force_pull or len(self.cli.images(name=self.image)) == 0:
            self.log.info('Pulling docker image %s', self.image)
            for l in self.cli.pull(self.image, stream=True):
                output = json.loads(l.decode('utf-8').strip())
                if 'status' in output:
                    self.log.info("%s", output['status'])

        with self.get_host_tmp_dir() as host_tmp_dir:
            self.environment['AIRFLOW_TMP_DIR'] = self.tmp_dir
            self.volumes.append('{0}:{1}'.format(host_tmp_dir, self.tmp_dir))

            self.container = self.cli.create_container(
                command=self.get_command(),
                environment=self.environment,
                host_config=self.cli.create_host_config(
                    auto_remove=self.auto_remove,
                    binds=self.volumes,
                    network_mode=self.network_mode,
                    shm_size=self.shm_size,
                    dns=self.dns,
                    dns_search=self.dns_search,
                    cpu_shares=int(round(self.cpus * 1024)),
                    mem_limit=self.mem_limit,
                ),
                image=self.image,
                user=self.user,
                working_dir=self.working_dir,
            )
            self.cli.start(self.container['Id'])

            line = ''
            for line in self.cli.logs(container=self.container['Id'], stream=True):
                line = line.strip()
                if hasattr(line, 'decode'):
                    line = line.decode('utf-8')
                self.log.info(line)

            result = self.cli.wait(self.container['Id'])
            if result['StatusCode'] != 0:
                raise AirflowException('docker container failed: ' + repr(result))

            if self.xcom_push_flag:
                return self.cli.logs(container=self.container['Id']) if self.xcom_all else str(line)

    # This is a class-private name on DockerOperator for no good reason --
    # all that the status quo does is inhibit extension of the class.
    # See https://issues.apache.org/jira/browse/AIRFLOW-3880
    def __get_tls_config(self):
        # pylint: disable=no-member
        return super(ModifiedDockerOperator, self)._DockerOperator__get_tls_config()


class DagsterDockerOperator(ModifiedDockerOperator, DagsterOperator):
    '''Dagster operator for Apache Airflow.

    Wraps a modified DockerOperator incorporating https://github.com/apache/airflow/pull/4315.

    Additionally, if a Docker client can be initialized using docker.from_env,
    Unlike the standard DockerOperator, this operator also supports config using docker.from_env,
    so it isn't necessary to explicitly set docker_url, tls_config, or api_version.

    '''

    # py2 compat
    # pylint: disable=keyword-arg-before-vararg
    def __init__(
        self,
        step=None,
        config=None,
        pipeline_name=None,
        step_keys=None,
        s3_bucket_name=None,
        *args,
        **kwargs
    ):
        self.step = step
        self.config = config
        self.pipeline_name = pipeline_name
        self.step_keys = step_keys
        self.docker_conn_id_set = kwargs.get('docker_conn_id') is not None
        self.s3_bucket_name = s3_bucket_name
        self._run_id = None

        # We don't use dagster.check here to avoid taking the dependency.
        for attr_ in ['config', 'pipeline_name']:
            assert isinstance(getattr(self, attr_), string_types), (
                'Bad value for DagsterDockerOperator {attr_}: expected a string and got {value} of '
                'type {type_}'.format(
                    attr_=attr_, value=getattr(self, attr_), type_=type(getattr(self, attr_))
                )
            )

        if self.step_keys is None:
            self.step_keys = []

        assert isinstance(self.step_keys, list), (
            'Bad value for DagsterDockerOperator step_keys: expected a list and got {value} of '
            'type {type_}'.format(value=self.step_keys, type_=type(self.step_keys))
        )

        bad_keys = []
        for ix, step_key in enumerate(self.step_keys):
            if not isinstance(step, string_types):
                bad_keys.append((ix, step_key))
        assert not bad_keys, (
            'Bad values for DagsterDockerOperator step_keys (expected only strings): '
            '{bad_values}'
        ).format(
            bad_values=', '.join(
                [
                    '{value} of type {type_} at index {idx}'.format(
                        value=bad_key[1], type_=type(bad_key[1]), idx=bad_key[0]
                    )
                    for bad_key in bad_keys
                ]
            )
        )

        # These shenanigans are so we can override DockerOperator.get_hook in order to configure
        # a docker client using docker.from_env, rather than messing with the logic of
        # DockerOperator.execute
        if not self.docker_conn_id_set:
            try:
                from_env().version()
            except:  # pylint: disable=bare-except
                pass
            else:
                kwargs['docker_conn_id'] = True

        # We do this because log lines won't necessarily be emitted in order (!) -- so we can't
        # just check the last log line to see if it's JSON.
        kwargs['xcom_all'] = True

        if 'network_mode' not in kwargs:
            # FIXME: this is not the best test to see if we're running on Docker for Mac
            kwargs['network_mode'] = 'host' if sys.platform != 'darwin' else 'bridge'

        if 'environment' not in kwargs:
            kwargs['environment'] = DEFAULT_ENVIRONMENT

        super(DagsterDockerOperator, self).__init__(*args, **kwargs)

    @classmethod
    def operator_for_solid(
        cls, pipeline, env_config, solid_name, step_keys, dag, dag_id, op_kwargs
    ):
        tmp_dir = op_kwargs.pop('tmp_dir', DOCKER_TEMPDIR)
        host_tmp_dir = op_kwargs.pop('host_tmp_dir', seven.get_system_temp_directory())

        if 'storage' not in env_config:
            raise AirflowException(
                'No storage config found -- must configure either filesystem or s3 storage for '
                'the DagsterPythonOperator. Ex.: \n'
                '{{\'storage\': {{\'filesystem\': {{\'base_dir\': \'{tmp_dir}\'}}}}}} or \n'
                '{{\'storage\': {{\'s3\': {{\'s3_bucket\': \'my-s3-bucket\'}}}}}}'.format(
                    tmp_dir=tmp_dir
                )
            )

        # black 18.9b0 doesn't support py27-compatible formatting of the below invocation (omitting
        # the trailing comma after **op_kwargs) -- black 19.3b0 supports multiple python versions,
        # but currently doesn't know what to do with from __future__ import print_function -- see
        # https://github.com/ambv/black/issues/768
        # fmt: off
        return DagsterDockerOperator(
            step=solid_name,
            config=format_config_for_graphql(env_config),
            dag=dag,
            tmp_dir=tmp_dir,
            pipeline_name=pipeline.name,
            step_keys=step_keys,
            task_id=solid_name,
            host_tmp_dir=host_tmp_dir,
            **op_kwargs
        )
        # fmt: on

    @property
    def run_id(self):
        if self._run_id is None:
            return ''
        else:
            return self._run_id

    @property
    def query(self):
        step_keys = '[{quoted_step_keys}]'.format(
            quoted_step_keys=', '.join(
                ['"{step_key}"'.format(step_key=step_key) for step_key in self.step_keys]
            )
        )
        return QUERY_TEMPLATE.format(
            config=self.config.strip('\n'),
            run_id=self.run_id,
            step_keys=step_keys,
            pipeline_name=self.pipeline_name,
        )

    def get_command(self):
        if self.command is not None and self.command.strip().find('[') == 0:
            commands = ast.literal_eval(self.command)
        elif self.command is not None:
            commands = self.command
        else:
            commands = DAGSTER_OPERATOR_COMMAND_TEMPLATE.format(query=self.query)
        return commands

    def get_hook(self):
        if self.docker_conn_id_set:
            return super(DagsterDockerOperator, self).get_hook()

        class _DummyHook(object):
            def get_conn(self):
                return from_env().api

        return _DummyHook()

    def execute(self, context):
        if 'run_id' in self.params:
            self._run_id = self.params['run_id']
        elif 'dag_run' in context and context['dag_run'] is not None:
            self._run_id = context['dag_run'].run_id

        try:
            self.log.debug('Executing with query: {query}'.format(query=self.query))

            raw_res = super(DagsterDockerOperator, self).execute(context)
            self.log.info('Finished executing container.')
            (res, last_line) = parse_raw_res(raw_res)

            handle_errors(res, last_line)

            res_data = res['data']['executePlan']

            res_type = res_data['__typename']

            if res_type == 'PipelineConfigValidationInvalid':
                errors = [err['message'] for err in res_data['errors']]
                raise AirflowException(
                    'Pipeline configuration invalid:\n{errors}'.format(errors='\n'.join(errors))
                )

            if res_type == 'PipelineNotFoundError':
                raise AirflowException(
                    'Pipeline {pipeline_name} not found: {message}:\n{stack_entries}'.format(
                        pipeline_name=res_data['pipelineName'],
                        message=res_data['message'],
                        stack_entries='\n'.join(res_data['stack']),
                    )
                )

            if res_type == 'ExecutePlanSuccess':
                self.log.info('Plan execution succeeded.')
                if res_data['hasFailures']:
                    errors = [
                        step['errorMessage']
                        for step in res_data['stepEvents']
                        if not step['success']
                    ]
                    raise AirflowException(
                        'Subplan execution failed:\n{errors}'.format(errors='\n'.join(errors))
                    )

                return res

            if res_type == 'PythonError':
                self.log.info('Plan execution failed.')
                raise AirflowException(
                    'Subplan execution failed: {message}\n{stack}'.format(
                        message=res_data['message'], stack=res_data['stack']
                    )
                )

            # Catchall
            return res

        finally:
            self._run_id = None

    # This is a class-private name on DockerOperator for no good reason --
    # all that the status quo does is inhibit extension of the class.
    # See https://issues.apache.org/jira/browse/AIRFLOW-3880
    def __get_tls_config(self):
        # pylint:disable=no-member
        return super(DagsterDockerOperator, self)._ModifiedDockerOperator__get_tls_config()

    @contextmanager
    def get_host_tmp_dir(self):
        yield self.host_tmp_dir


class DagsterPythonOperator(PythonOperator, DagsterOperator):
    @classmethod
    def make_python_callable(cls, dag_id, pipeline, env_config, step_keys):
        try:
            from dagster import RepositoryDefinition
            from dagster.cli.dynamic_loader import RepositoryContainer
            from dagit.cli import execute_query_from_cli
        except ImportError:
            raise AirflowException(
                'To use the DagsterPythonOperator, dagster and dagit must be installed in your '
                'Airflow environment.'
            )
        repository = RepositoryDefinition('<<ephemeral repository>>', {dag_id: lambda: pipeline})
        repository_container = RepositoryContainer(repository=repository)

        def python_callable(**kwargs):
            run_id = kwargs.get('dag_run').run_id
            query = QUERY_TEMPLATE.format(
                config=env_config,
                run_id=run_id,
                step_keys=json.dumps(step_keys),
                pipeline_name=pipeline.name,
            )
            return json.loads(execute_query_from_cli(repository_container, query, variables=None))

        return python_callable

    @classmethod
    def operator_for_solid(
        cls, pipeline, env_config, solid_name, step_keys, dag, dag_id, op_kwargs
    ):
        if 'storage' not in env_config:
            raise AirflowException(
                'No storage config found -- must configure either filesystem or s3 storage for '
                'the DagsterPythonOperator. Ex.: \n'
                '{\'storage\': {\'filesystem\': {\'base_dir\': \'/tmp/special_place\'}}} or \n'
                '{\'storage\': {\'s3\': {\'s3_bucket\': \'my-s3-bucket\'}}}'
            )

        # black 18.9b0 doesn't support py27-compatible formatting of the below invocation (omitting
        # the trailing comma after **op_kwargs) -- black 19.3b0 supports multiple python versions, but
        # currently doesn't know what to do with from __future__ import print_function -- see
        # https://github.com/ambv/black/issues/768
        # fmt: off
        return PythonOperator(
            task_id=solid_name,
            provide_context=True,
            python_callable=cls.make_python_callable(
                dag_id, pipeline, format_config_for_graphql(env_config), step_keys
            ),
            dag=dag,
            **op_kwargs
        )
        # fmt: on
