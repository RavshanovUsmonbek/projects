import json
from datetime import datetime
from queue import Empty
from typing import Optional, Union, Tuple
from flask_restful import Resource
from flask import request, g
from pylon.core.tools import log

from tools import auth, constants as c, secrets_tools

from ...models.project import Project
from ...models.statistics import Statistic
from ...models.quota import ProjectQuota
from ...tools.influx_tools import create_project_databases, drop_project_databases


class API(Resource):
    url_params = [
        '',
        '<int:project_id>',
    ]

    def __init__(self, module):
        self.module = module

    # @auth.decorators.check_api(['global_view'])
    def get(self, project_id: Optional[int] = None) -> Union[Tuple[dict, int], Tuple[list, int]]:
        offset_ = request.args.get("offset")
        limit_ = request.args.get("limit")
        search_ = request.args.get("search")
        return self.module.list(
            offset_=offset_, limit_=limit_, search_=search_
        ), 200

    # @auth.decorators.check_api(['global_view'])
    def post(self, project_id: Optional[int] = None) -> Tuple[dict, int]:
        log.info('request received')
        log.info('do we have an rpc? %s', self.module.context.rpc_manager)
        data = request.json
        name_ = data["name"]
        # owner_ = data["owner"]
        owner_ = str(g.auth.id)
        vuh_limit = data["vuh_limit"]
        plugins = data["plugins"]
        storage_space_limit = data["storage_space_limit"]
        data_retention_limit = data["data_retention_limit"]
        invitations = data['invitations']
        project = Project(
            name=name_,
            plugins=plugins,
            project_owner=owner_
        )
        project_secrets = {}
        project_hidden_secrets = {}
        project.insert()
        log.info('after project.insert()')

        try:
            self.module.context.rpc_manager.timeout(2).project_keycloak_group_handler(project).send_invitations(
                invitations)
        except Empty:
            ...

        log.info('after invitations sent')
        # SessionProject.set(project.id)  # Looks weird, sorry :D
        ProjectQuota.create(project.id, vuh_limit, storage_space_limit, data_retention_limit)
        log.info('after quota created')
        statistic = Statistic(
            project_id=project.id,
            start_time=str(datetime.utcnow()),
            vuh_used=0,
            performance_test_runs=0,
            sast_scans=0,
            dast_scans=0,
            ui_performance_test_runs=0,
            public_pool_workers=0,
            tasks_executions=0
        )
        statistic.insert()
        log.info('after statistic created')
        pp_args = {
            "funcname": "post_processor",
            "invoke_func": "lambda_function.lambda_handler",
            "runtime": "Python 3.7",
            "region": "default",
            "env_vars": json.dumps({
                "jmeter_db": "{{secret.jmeter_db}}",
                "gatling_db": "{{secret.gatling_db}}",
                "comparison_db": "{{secret.comparison_db}}"
            })
        }
        pp = self.module.context.rpc_manager.call.task_create(project, c.POST_PROCESSOR_PATH, pp_args)
        log.info('after pp task created')
        cc_args = {
            "funcname": "control_tower",
            "invoke_func": "lambda.handler",
            "runtime": "Python 3.7",
            "region": "default",
            "env_vars": json.dumps({
                "token": "{{secret.auth_token}}",
                "galloper_url": "{{secret.galloper_url}}",
                "GALLOPER_WEB_HOOK": '{{secret.post_processor}}',
                "project_id": '{{secret.project_id}}',
                "loki_host": '{{secret.loki_host}}'
            })
        }
        cc = self.module.context.rpc_manager.call.task_create(project, c.CONTROL_TOWER_PATH, cc_args)
        log.info('after cc task created')
        project_secrets["galloper_url"] = c.APP_HOST
        project_secrets["project_id"] = project.id
        project_hidden_secrets["post_processor"] = f'{c.APP_HOST}{pp.webhook}'
        project_hidden_secrets["post_processor_id"] = pp.task_id
        project_hidden_secrets["redis_host"] = c.APP_IP
        project_hidden_secrets["loki_host"] = c.EXTERNAL_LOKI_HOST.replace("https://", "http://")
        project_hidden_secrets["influx_ip"] = c.APP_IP
        project_hidden_secrets["influx_port"] = c.INFLUX_PORT
        project_hidden_secrets["loki_port"] = c.LOKI_PORT
        project_hidden_secrets["redis_password"] = c.REDIS_PASSWORD
        project_hidden_secrets["rabbit_host"] = c.APP_IP
        project_hidden_secrets["rabbit_user"] = c.RABBIT_USER
        project_hidden_secrets["rabbit_password"] = c.RABBIT_PASSWORD
        project_hidden_secrets["control_tower_id"] = cc.task_id
        project_hidden_secrets["influx_user"] = c.INFLUX_USER
        project_hidden_secrets["influx_password"] = c.INFLUX_PASSWORD
        project_hidden_secrets["jmeter_db"] = f'jmeter_{project.id}'
        project_hidden_secrets["gatling_db"] = f'gatling_{project.id}'
        project_hidden_secrets["comparison_db"] = f'comparison_{project.id}'
        project_hidden_secrets["telegraf_db"] = f'telegraf_{project.id}'
        project_hidden_secrets["gf_api_key"] = c.GF_API_KEY

        project_vault_data = {
            "auth_role_id": "",
            "auth_secret_id": ""
        }
        try:
            project_vault_data = secrets_tools.init_project_space(project.id)
        except:
            log.warning("Vault is not configured")
        log.info('after init_project space')
        project.secrets_json = {
            "vault_auth_role_id": project_vault_data["auth_role_id"],
            "vault_auth_secret_id": project_vault_data["auth_secret_id"],
        }
        project.worker_pool_config_json = {
            "regions": ["default"]
        }
        project.commit()

        secrets_tools.set_project_secrets(project.id, project_secrets)
        log.info('after set_project_secrets')
        secrets_tools.set_project_hidden_secrets(project.id, project_hidden_secrets)
        log.info('after set_project_hidden_secrets')
        create_project_databases(project.id)
        log.info('after create_project_databases')

        # set_grafana_datasources(project.id)
        return project.to_json(exclude_fields=Project.API_EXCLUDE_FIELDS), 201

    # @auth.decorators.check_api(['global_view'])
    def put(self, project_id: Optional[int] = None) -> Tuple[dict, int]:
        # data = self._parser_post.parse_args()
        data = request.json
        if not project_id:
            return {"message": "Specify project id"}, 400
        project = Project.get_or_404(project_id)
        if data["name"]:
            project.name = data["name"]
        if data["owner"]:
            project.project_owner = data["owner"]
        if data["plugins"]:
            project.plugins = data["plugins"]
        project.commit()
        return project.to_json(exclude_fields=Project.API_EXCLUDE_FIELDS), 200

    # @auth.decorators.check_api(['global_view'])
    def delete(self, project_id: int) -> Tuple[dict, int]:
        drop_project_databases(project_id)
        Project.apply_full_delete_by_pk(pk=project_id)
        secrets_tools.remove_project_space(project_id)
        return {"message": f"Project with id {project_id} was successfully deleted"}, 204