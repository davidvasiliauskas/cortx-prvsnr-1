import sys
import attr
from typing import List, Dict, Type, Union
from copy import deepcopy
import logging
from pathlib import Path

from .errors import BadPillarDataError
from .config import (
    ALL_MINIONS, PRVSNR_USER_FILES_EOSUPDATE_REPOS_DIR,
    PRVSNR_FILEROOTS_DIR, LOCAL_MINION,
    PRVSNR_USER_FILES_SSL_CERTS_FILE,
    PRVSNR_EOS_COMPONENTS,
    CONTROLLER_BOTH
)
from .utils import load_yaml, dump_yaml_str
from .param import KeyPath, Param
from .pillar import PillarUpdater, PillarResolver
from .api_spec import api_spec
from .salt import (
    StatesApplier, StateFunExecuter, State,
    YumRollbackManager,
    SaltJobsRunner, function_run
)
from .hare import (
    ensure_cluster_is_stopped, ensure_cluster_is_started
)
from provisioner import inputs
from provisioner import values

_mod = sys.modules[__name__]
logger = logging.getLogger(__name__)


class RunArgs:
    targets: str = attr.ib(
        default=ALL_MINIONS,
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "command's host targets"
            }
        }
    )
    dry_run: bool = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "perform validation only"
            }
        }, default=False
    )


@attr.s(auto_attribs=True)
class RunArgsEmpty:
    pass


@attr.s(auto_attribs=True)
class RunArgsBase:
    targets: str = RunArgs.targets


@attr.s(auto_attribs=True)
class RunArgsUpdate:
    targets: str = RunArgs.targets
    dry_run: bool = RunArgs.dry_run

# TODO DRY
@attr.s(auto_attribs=True)
class RunArgsFWUpdate:
    source: str = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "a path to FW update"
            }
        }
    )
    dry_run: bool = RunArgs.dry_run


@attr.s(auto_attribs=True)
class RunArgsGetResult:
    cmd_id: str = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "provisioner command ID"
            }
        }
    )


@attr.s(auto_attribs=True)
class RunArgsSSLCerts:
    source: str = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "ssl certs source"
            }
        }
    )
    restart: bool = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "restart flag"
            }
        }, default=False
    )
    dry_run: bool = RunArgs.dry_run


@attr.s(auto_attribs=True)
class RunArgsConfigureEOS:
    component: str = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "EOS component to configure",
                'choices': PRVSNR_EOS_COMPONENTS
            }
        }
    )
    source: str = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "a yaml file to apply"
            }
        },
        default=None
    )
    show: bool = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "dump current configuration"
            }
        }, default=False
    )
    reset: bool = attr.ib(
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "reset configuration to the factory state"
            }
        }, default=False
    )


@attr.s(auto_attribs=True)
class RunArgsController:
    target_ctrl: str = attr.ib(
        default=CONTROLLER_BOTH,
        metadata={
            inputs.METADATA_ARGPARSER: {
                'help': "target controller"
                # TODO IMPROVE use argparse choises to limit
                #      valid vales only to a/b/both
            }
        }
    )


class CommandParserFillerMixin:
    _run_args_type = RunArgsBase

    @classmethod
    def fill_parser(cls, parser):
        inputs.ParserFiller.fill_parser(cls._run_args_type, parser)

    @classmethod
    def from_spec(cls):
        return cls()

    @classmethod
    def extract_positional_args(cls, kwargs):
        return inputs.ParserFiller.extract_positional_args(
            cls._run_args_type, kwargs
        )


#  - Notes:
#       1. call salt pillar is good since salt will expand
#          properly pillar itself
#       2. if pillar != system state then we are bad
#           - then assume they are in-sync
#  - ? what are cases when pillar != system
#  - ? options to check/ensure sync:
#     - salt.mine
#     - periodical states apply
@attr.s(auto_attribs=True)
class PillarGet(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams

    def run(self, targets: str = ALL_MINIONS):
        return PillarResolver(targets=targets).pillar


@attr.s(auto_attribs=True)
class Get(CommandParserFillerMixin):
    params_type: Type[inputs.ParamsList] = inputs.ParamsList

    # TODO input class type
    @classmethod
    def from_spec(
        cls, params_type: str = 'ParamsList'
    ):
        return cls(params_type=getattr(inputs, params_type))

    def run(self, *args, targets=ALL_MINIONS, **kwargs):
        # TODO tests
        params = self.params_type.from_args(*args, **kwargs)
        pillar_resolver = PillarResolver(targets=targets)
        res_raw = pillar_resolver.get(params)
        res = {}
        for minion_id, data in res_raw.items():
            res[minion_id] = {str(p.name): v for p, v in data.items()}
        return res


# TODO
#   - how to support targetted pillar
#       - per group (grains)
#       - per minion
#       - ...
#
# Implements the following:
#   - update pillar related to some param(s)
#   - call related states (before and after)
#   - rollback if something goes wrong
@attr.s(auto_attribs=True)
class Set(CommandParserFillerMixin):
    # TODO at least either pre or post should be defined
    params_type: Type[
        Union[inputs.ParamGroupInputBase, inputs.ParamDictItemInputBase]
    ]
    pre_states: List[State] = attr.Factory(list)
    post_states: List[State] = attr.Factory(list)

    _run_args_type = RunArgsUpdate

    # TODO input class type
    @classmethod
    def from_spec(
        cls, params_type: str, states: Dict
    ):
        return cls(
            params_type=getattr(inputs, params_type),
            pre_states=[State(state) for state in states.get('pre', [])],
            post_states=[State(state) for state in states.get('post', [])]
        )

    def _run(self, params, targets):
        pillar_updater = PillarUpdater(targets)

        pillar_updater.update(params)
        try:
            StatesApplier.apply(self.pre_states)
            try:
                pillar_updater.apply()
                StatesApplier.apply(self.post_states)
            except Exception:
                logger.exception('Failed to apply changes')
                # TODO more solid rollback
                pillar_updater.rollback()
                pillar_updater.apply()
                raise
        except Exception:
            logger.exception('Failed to apply changes')
            # treat post as restoration for pre, apply
            # if rollback happened
            StatesApplier.apply(self.post_states)
            raise

    # TODO
    # - class for pillar file
    # - caching (load once)
    def run(self, *args, targets=ALL_MINIONS, dry_run=False, **kwargs):
        # static validation
        if len(args) == 1 and isinstance(args[0], self.params_type):
            params = args[0]
        else:
            params = self.params_type.from_args(*args, **kwargs)

        # TODO dynamic validation
        if dry_run:
            return

        self._run(params, targets)


# assumtions / limitations
#   - support only for ALL_MINIONS targetting TODO ??? why do you think so
#
#

# set/remove the repo:
#   - call repo reset logic for minions:
#       - remove repo config for yum
#       - unmount repo if needed
#       - remove repo dir/iso file if needed TODO
#   - call repo reset logic for master:
#       - remove local dir/file from salt user file root (if needed)
@attr.s(auto_attribs=True)
class SetEOSUpdateRepo(Set):
    # TODO at least either pre or post should be defined
    params_type: Type[inputs.EOSUpdateRepo] = inputs.EOSUpdateRepo

    # TODO rollback
    def _run(self, params: inputs.EOSUpdateRepo, targets: str):
        # if local - copy the repo to salt user file root
        if params.is_local():
            dest = PRVSNR_USER_FILES_EOSUPDATE_REPOS_DIR / params.release

            # TODO consider to use symlink instead

            if params.is_dir():
                # TODO
                #  - file.recurse expects only dirs from maste file roots
                #    (salt://), need to find another alternative to respect
                #    indempotence
                # StateFunExecuter.execute(
                #     'file.recurse',
                #     fun_kwargs=dict(
                #       source=str(params.source),
                #       name=str(dest)
                #     )
                # )
                StateFunExecuter.execute(
                    'cmd.run',
                    fun_kwargs=dict(
                        name=(
                            "mkdir -p {0} && rm -rf {2} && cp -R {1} {2}"
                            .format(dest.parent, params.source, dest)
                        )
                    )
                )
            else:  # iso file
                StateFunExecuter.execute(
                    'file.managed',
                    fun_kwargs=dict(
                        source=str(params.source),
                        name='{}.iso'.format(dest),
                        makedirs=True
                    )
                )

        # call default set logic (set pillar, call related states)
        super()._run(params, targets)


# TODO consider to use RunArgsUpdate and support dry-run
@attr.s(auto_attribs=True)
class EOSUpdate(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams

    def run(self, targets):
        # TODO:
        #   - create a state instead
        #   - what about apt and other non-yum pkd managers
        #   (downgrade is another more generic option but it requires
        #    exploration of depednecies that are updated)
        try:
            with YumRollbackManager(targets, multiple_targets_ok=True):
                # TODO
                #  - update for provisioner itself
                #  - update for other sw ???
                ensure_cluster_is_stopped()
                for component in (
                    'eoscore', 's3server', 'hare', 'sspl', 'csm'
                ):
                    state_name = "components.{}.update".format(component)
                    try:
                        logger.info(
                            "Updating {} on {}".format(component, targets)
                        )
                        StatesApplier.apply([state_name], targets)
                    except Exception:
                        logger.exception(
                            "Failed to update {} on {}"
                            .format(component, targets)
                        )
                        raise
                ensure_cluster_is_started()
        except Exception:
            logger.exception('Update failed')
            try:
                ensure_cluster_is_started()
            except Exception:
                logger.exception('Failed to start cluster after failed update')
            # TODO IMPROVE
            raise


# TODO TEST
@attr.s(auto_attribs=True)
class FWUpdate(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsFWUpdate

    def run(self, source, dry_run=False):
        source = Path(source).resolve()

        if not source.is_file():
            raise ValueError('{} is not a file'.format(source))

        script = (
            PRVSNR_FILEROOTS_DIR /
            'components/controller/files/scripts/controller-cli.sh'
        )
        controller_pi_path = KeyPath('cluster/storage_enclosure/controller')
        ip = Param('ip', 'cluster.sls', controller_pi_path / 'primary_mc/ip')
        user = Param('user', 'cluster.sls', controller_pi_path / 'user')
        passwd = Param('passwd', 'cluster.sls', controller_pi_path / 'secret')
        pillar = PillarResolver(LOCAL_MINION).get([ip, user, passwd])
        pillar = next(iter(pillar.values()))

        for param in (ip, user, passwd):
            if not pillar[param] or pillar[param] is values.MISSED:
                raise BadPillarDataError(
                    'value for {} is not specified'.format(param.pi_key)
                )

        if dry_run:
            return

        StateFunExecuter.execute(
            'cmd.run',
            fun_kwargs=dict(
                name=(
                    "{script} host -h {ip} -u {user} -p {passwd} "
                    "--update-fw {source}"
                    .format(
                        script=script,
                        ip=pillar[ip],
                        user=pillar[user],
                        passwd=pillar[passwd],
                        source=source
                    )
                )
            )
        )


@attr.s(auto_attribs=True)
class GetResult(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsGetResult

    def run(self, cmd_id: str):
        return SaltJobsRunner.prvsnr_job_result(cmd_id)


# TODO TEST
@attr.s(auto_attribs=True)
class GetClusterId(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsEmpty

    def run(self):
        return list(function_run(
            'grains.get',
            fun_args=['cluster_id']
        ).values())[0]


# TODO TEST
@attr.s(auto_attribs=True)
class GetNodeId(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsBase

    def run(self, targets):
        return function_run(
            'grains.get',
            fun_args=['node_id'],
            targets=targets
        )

# TODO TEST
# TODO consider to use RunArgsUpdate and support dry-run
@attr.s(auto_attribs=True)
class SetSSLCerts(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsSSLCerts

    def run(self, source, restart=False, dry_run=False):

        source = Path(source).resolve()

        if not source.is_file():
            raise ValueError('{} is not a file'.format(source))

        if dry_run:
            return

        state_name = "components.misc_pkgs.ssl_certs"
        dest = PRVSNR_USER_FILES_SSL_CERTS_FILE
        # TODO create backup and add timestamp to backups
        StateFunExecuter.execute(
            "file.managed",
            fun_kwargs=dict(
                source=str(source),
                name=str(dest),
                makedirs=True
            )
        )

        try:
            StatesApplier.apply([state_name])
        except Exception:
            logger.exception(
                "Failed to apply certs"
            )
            raise


# TODO TEST
@attr.s(auto_attribs=True)
class RebootServer(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsBase

    def run(self, targets):
        return function_run(
            'system.reboot',
            targets=targets
        )


# TODO IMPROVE dry-run mode
@attr.s(auto_attribs=True)
class RebootController(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsController

    @classmethod
    def from_spec(cls):
        return cls()

    def run(self, target_ctrl: str = CONTROLLER_BOTH):

        script = (
            PRVSNR_FILEROOTS_DIR /
            'components/controller/files/scripts/controller-cli.sh'
        )
        controller_pi_path = KeyPath('cluster/storage_enclosure/controller')
        ip = Param('ip', 'cluster.sls', controller_pi_path / 'primary_mc/ip')
        user = Param('user', 'cluster.sls', controller_pi_path / 'user')
        passwd = Param('passwd', 'cluster.sls', controller_pi_path / 'secret')
        pillar = PillarResolver(LOCAL_MINION).get([ip, user, passwd])
        pillar = next(iter(pillar.values()))

        for param in (ip, user, passwd):
            if not pillar[param] or pillar[param] is values.MISSED:
                raise BadPillarDataError(
                    'value for {} is not specified'.format(param.pi_key)
                )

        StateFunExecuter.execute(
            'cmd.run',
            fun_kwargs=dict(
                name=(
                    "{script} host -h {ip} -u {user} -p {passwd} "
                    "--restart-ctrl {target_ctrl}"
                    .format(
                        script=script,
                        ip=pillar[ip],
                        user=pillar[user],
                        passwd=pillar[passwd],
                        target_ctrl=target_ctrl
                    )
                )
            )
        )


# TODO IMPROVE dry-run mode
@attr.s(auto_attribs=True)
class ShutdownController(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsController

    @classmethod
    def from_spec(cls):
        return cls()

    def run(self, target_ctrl: str = CONTROLLER_BOTH):

        script = (
            PRVSNR_FILEROOTS_DIR /
            'components/controller/files/scripts/controller-cli.sh'
        )
        controller_pi_path = KeyPath('cluster/storage_enclosure/controller')
        ip = Param('ip', 'cluster.sls', controller_pi_path / 'primary_mc/ip')
        user = Param('user', 'cluster.sls', controller_pi_path / 'user')
        # TODO IMPROVE improve Param to hide secrets
        passwd = Param('passwd', 'cluster.sls', controller_pi_path / 'secret')
        pillar = PillarResolver(LOCAL_MINION).get([ip, user, passwd])
        pillar = next(iter(pillar.values()))

        for param in (ip, user, passwd):
            if not pillar[param] or pillar[param] is values.MISSED:
                raise BadPillarDataError(
                    'value for {} is not specified'.format(param.pi_key)
                )

        StateFunExecuter.execute(
            'cmd.run',
            fun_kwargs=dict(
                name=(
                    "{script} host -h {ip} -u {user} -p {passwd} "
                    "--shutdown-ctrl {target_ctrl}"
                    .format(
                        script=script,
                        ip=pillar[ip],
                        user=pillar[user],
                        passwd=pillar[passwd],
                        target_ctrl=target_ctrl
                    )
                )
            )
        )


@attr.s(auto_attribs=True)
class ConfigureEOS(CommandParserFillerMixin):
    params_type: Type[inputs.NoParams] = inputs.NoParams
    _run_args_type = RunArgsConfigureEOS

    def run(
        self, component, source=None, show=False, reset=False
    ):
        if source and not (show or reset):
            pillar = load_yaml(source)
        else:
            pillar = None

        res = PillarUpdater().component_pillar(
            component, show=show, reset=reset, pillar=pillar
        )

        if show:
            print(dump_yaml_str(res))


commands = {}
for command_name, spec in api_spec.items():
    spec = deepcopy(api_spec[command_name])  # TODO
    command = getattr(_mod, spec.pop('type'))
    commands[command_name] = command.from_spec(**spec)
