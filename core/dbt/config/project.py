from copy import deepcopy
from dataclasses import dataclass, field
from itertools import chain
from typing import (
    List, Dict, Any, Optional, TypeVar, Union, Tuple, Callable, Mapping,
)
from typing_extensions import Protocol, runtime_checkable

import hashlib
import os

from dbt.clients.system import resolve_path_from_base
from dbt.clients.system import path_exists
from dbt.clients.system import load_file_contents
from dbt.clients.yaml_helper import load_yaml_text
from dbt.contracts.connection import QueryComment
from dbt.exceptions import DbtProjectError
from dbt.exceptions import RecursionException
from dbt.exceptions import SemverException
from dbt.exceptions import validator_error_message
from dbt.exceptions import RuntimeException
from dbt.graph import SelectionSpec
from dbt.helper_types import NoValue
from dbt.semver import VersionSpecifier
from dbt.semver import versions_compatible
from dbt.version import get_installed_version
from dbt.utils import deep_map, MultiDict
from dbt.node_types import NodeType

from dbt.contracts.project import (
    Project as ProjectContract,
    SemverString,
)
from dbt.contracts.project import PackageConfig

from hologram import ValidationError

from .renderer import DbtProjectYamlRenderer
from .selectors import (
    selector_config_from_data,
    selector_data_from_root,
    SelectorConfig,
)


INVALID_VERSION_ERROR = """\
This version of dbt is not supported with the '{package}' package.
  Installed version of dbt: {installed}
  Required version of dbt for '{package}': {version_spec}
Check the requirements for the '{package}' package, or run dbt again with \
--no-version-check
"""


IMPOSSIBLE_VERSION_ERROR = """\
The package version requirement can never be satisfied for the '{package}
package.
  Required versions of dbt for '{package}': {version_spec}
Check the requirements for the '{package}' package, or run dbt again with \
--no-version-check
"""

MALFORMED_PACKAGE_ERROR = """\
The packages.yml file in this project is malformed. Please double check
the contents of this file and fix any errors before retrying.

You can find more information on the syntax for this file here:
https://docs.getdbt.com/docs/package-management

Validator Error:
{error}
"""


@runtime_checkable
class IsFQNResource(Protocol):
    fqn: List[str]
    resource_type: NodeType
    package_name: str


def _list_if_none(value):
    if value is None:
        value = []
    return value


def _dict_if_none(value):
    if value is None:
        value = {}
    return value


def _list_if_none_or_string(value):
    value = _list_if_none(value)
    if isinstance(value, str):
        return [value]
    return value


def _load_yaml(path):
    contents = load_file_contents(path)
    return load_yaml_text(contents)


def package_data_from_root(project_root):
    package_filepath = resolve_path_from_base(
        'packages.yml', project_root
    )

    if path_exists(package_filepath):
        packages_dict = _load_yaml(package_filepath)
    else:
        packages_dict = None
    return packages_dict


def package_config_from_data(packages_data):
    if packages_data is None:
        packages_data = {'packages': []}

    try:
        packages = PackageConfig.from_dict(packages_data)
    except ValidationError as e:
        raise DbtProjectError(
            MALFORMED_PACKAGE_ERROR.format(error=str(e.message))
        ) from e
    return packages


def _parse_versions(versions: Union[List[str], str]) -> List[VersionSpecifier]:
    """Parse multiple versions as read from disk. The versions value may be any
    one of:
        - a single version string ('>0.12.1')
        - a single string specifying multiple comma-separated versions
            ('>0.11.1,<=0.12.2')
        - an array of single-version strings (['>0.11.1', '<=0.12.2'])

    Regardless, this will return a list of VersionSpecifiers
    """
    if isinstance(versions, str):
        versions = versions.split(',')
    return [VersionSpecifier.from_version_string(v) for v in versions]


def _all_source_paths(
    source_paths: List[str],
    data_paths: List[str],
    snapshot_paths: List[str],
    analysis_paths: List[str],
    macro_paths: List[str],
) -> List[str]:
    return list(chain(source_paths, data_paths, snapshot_paths, analysis_paths,
                      macro_paths))


T = TypeVar('T')


def value_or(value: Optional[T], default: T) -> T:
    if value is None:
        return default
    else:
        return value


def _raw_project_from(project_root: str) -> Dict[str, Any]:

    project_root = os.path.normpath(project_root)
    project_yaml_filepath = os.path.join(project_root, 'dbt_project.yml')

    # get the project.yml contents
    if not path_exists(project_yaml_filepath):
        raise DbtProjectError(
            'no dbt_project.yml found at expected path {}'
            .format(project_yaml_filepath)
        )

    project_dict = _load_yaml(project_yaml_filepath)

    if not isinstance(project_dict, dict):
        raise DbtProjectError(
            'dbt_project.yml does not parse to a dictionary'
        )

    return project_dict


def _query_comment_from_cfg(
        cfg_query_comment: Union[QueryComment, NoValue, str, None]
) -> QueryComment:
    if not cfg_query_comment:
        return QueryComment(comment='')

    if isinstance(cfg_query_comment, str):
        return QueryComment(comment=cfg_query_comment)

    if isinstance(cfg_query_comment, NoValue):
        return QueryComment()

    return cfg_query_comment


@dataclass
class PartialProject:
    config_version: int = field(metadata=dict(
        description='The version of the configuration file format'
    ))
    profile_name: Optional[str] = field(metadata=dict(
        description='The unrendered profile name in the project, if set'
    ))
    project_name: Optional[str] = field(metadata=dict(
        description=(
            'The name of the project. This should always be set and will not '
            'be rendered'
        )
    ))
    project_root: str = field(
        metadata=dict(description='The root directory of the project'),
    )
    project_dict: Dict[str, Any]
    verify_version: bool = field(
        metadata=dict(description=(
            'If True, verify the dbt version matches the required version'
        ))
    )

    def render(self, renderer):
        packages_dict = package_data_from_root(self.project_root)
        selectors_dict = selector_data_from_root(self.project_root)
        return Project.render_from_dict(
            self.project_root,
            self.project_dict,
            packages_dict,
            selectors_dict,
            renderer,
            verify_version=self.verify_version,
        )

    def render_profile_name(self, renderer) -> Optional[str]:
        if self.profile_name is None:
            return None
        return renderer.render_value(self.profile_name)


class VarProvider:
    """Var providers are tied to a particular Project."""
    def __init__(
        self,
        vars: Dict[str, Dict[str, Any]]
    ) -> None:
        self.vars = vars

    def vars_for(
        self, node: IsFQNResource, adapter_type: str
    ) -> Mapping[str, Any]:
        # in v2, vars are only either project or globally scoped
        merged = MultiDict([self.vars])
        merged.add(self.vars.get(node.package_name, {}))
        return merged

    def to_dict(self):
        return self.vars


def validate_version(
    required: List[VersionSpecifier],
    project_name: str,
) -> None:
    """Ensure this package works with the installed version of dbt."""
    installed = get_installed_version()
    if not versions_compatible(*required):
        msg = IMPOSSIBLE_VERSION_ERROR.format(
            package=project_name,
            version_spec=[
                x.to_version_string() for x in required
            ]
        )
        raise DbtProjectError(msg)

    if not versions_compatible(installed, *required):
        msg = INVALID_VERSION_ERROR.format(
            package=project_name,
            installed=installed.to_version_string(),
            version_spec=[
                x.to_version_string() for x in required
            ]
        )
        raise DbtProjectError(msg)


@dataclass
class Project:
    project_name: str
    version: Union[SemverString, float]
    project_root: str
    profile_name: Optional[str]
    source_paths: List[str]
    macro_paths: List[str]
    data_paths: List[str]
    test_paths: List[str]
    analysis_paths: List[str]
    docs_paths: List[str]
    asset_paths: List[str]
    target_path: str
    snapshot_paths: List[str]
    clean_targets: List[str]
    log_path: str
    modules_path: str
    quoting: Dict[str, Any]
    models: Dict[str, Any]
    on_run_start: List[str]
    on_run_end: List[str]
    seeds: Dict[str, Any]
    snapshots: Dict[str, Any]
    sources: Dict[str, Any]
    vars: VarProvider
    dbt_version: List[VersionSpecifier]
    packages: Dict[str, Any]
    selectors: SelectorConfig
    query_comment: QueryComment
    config_version: int

    @property
    def all_source_paths(self) -> List[str]:
        return _all_source_paths(
            self.source_paths, self.data_paths, self.snapshot_paths,
            self.analysis_paths, self.macro_paths
        )

    @staticmethod
    def _preprocess(project_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Pre-process certain special keys to convert them from None values
        into empty containers, and to turn strings into arrays of strings.
        """
        handlers: Dict[Tuple[Union[str, int], ...], Callable[[Any], Any]] = {
            ('on-run-start',): _list_if_none_or_string,
            ('on-run-end',): _list_if_none_or_string,
        }

        for k in ('models', 'seeds', 'snapshots'):
            handlers[(k,)] = _dict_if_none
            handlers[(k, 'vars')] = _dict_if_none
            handlers[(k, 'pre-hook')] = _list_if_none_or_string
            handlers[(k, 'post-hook')] = _list_if_none_or_string
        handlers[('seeds', 'column_types')] = _dict_if_none

        def converter(value: Any, keypath: Tuple[Union[str, int], ...]) -> Any:
            if keypath in handlers:
                handler = handlers[keypath]
                return handler(value)
            else:
                return value

        return deep_map(converter, project_dict)

    @classmethod
    def from_project_config(
        cls,
        project_dict: Dict[str, Any],
        packages_dict: Optional[Dict[str, Any]] = None,
        selectors_dict: Optional[Dict[str, Any]] = None,
        required_dbt_version: Optional[List[VersionSpecifier]] = None,
    ) -> 'Project':
        """Create a project from its project and package configuration, as read
        by yaml.safe_load().

        :param project_dict: The dictionary as read from disk
        :param packages_dict: If it exists, the packages file as
            read from disk.
        :raises DbtProjectError: If the project is missing or invalid, or if
            the packages file exists and is invalid.
        :returns: The project, with defaults populated.
        """
        if required_dbt_version is None:
            dbt_version = cls._get_required_version(project_dict)
        else:
            dbt_version = required_dbt_version

        try:
            project_dict = cls._preprocess(project_dict)
        except RecursionException:
            raise DbtProjectError(
                'Cycle detected: Project input has a reference to itself',
                project=project_dict
            )
        try:
            cfg = ProjectContract.from_dict(project_dict)
        except ValidationError as e:
            raise DbtProjectError(validator_error_message(e)) from e

        # name/version are required in the Project definition, so we can assume
        # they are present
        name = cfg.name
        version = cfg.version
        # this is added at project_dict parse time and should always be here
        # once we see it.
        if cfg.project_root is None:
            raise DbtProjectError('cfg must have a project root!')
        else:
            project_root = cfg.project_root
        # this is only optional in the sense that if it's not present, it needs
        # to have been a cli argument.
        profile_name = cfg.profile
        # these are all the defaults
        source_paths: List[str] = value_or(cfg.source_paths, ['models'])
        macro_paths: List[str] = value_or(cfg.macro_paths, ['macros'])
        data_paths: List[str] = value_or(cfg.data_paths, ['data'])
        test_paths: List[str] = value_or(cfg.test_paths, ['test'])
        analysis_paths: List[str] = value_or(cfg.analysis_paths, [])
        snapshot_paths: List[str] = value_or(cfg.snapshot_paths, ['snapshots'])

        all_source_paths: List[str] = _all_source_paths(
            source_paths, data_paths, snapshot_paths, analysis_paths,
            macro_paths
        )

        docs_paths: List[str] = value_or(cfg.docs_paths, all_source_paths)
        asset_paths: List[str] = value_or(cfg.asset_paths, [])
        target_path: str = value_or(cfg.target_path, 'target')
        clean_targets: List[str] = value_or(cfg.clean_targets, [target_path])
        log_path: str = value_or(cfg.log_path, 'logs')
        modules_path: str = value_or(cfg.modules_path, 'dbt_modules')
        # in the default case we'll populate this once we know the adapter type
        # It would be nice to just pass along a Quoting here, but that would
        # break many things
        quoting: Dict[str, Any] = {}
        if cfg.quoting is not None:
            quoting = cfg.quoting.to_dict()

        models: Dict[str, Any]
        seeds: Dict[str, Any]
        snapshots: Dict[str, Any]
        sources: Dict[str, Any]
        vars_value: VarProvider

        if cfg.config_version != 2:
            raise ValidationError(
                f'Got unsupported config_version={cfg.config_version}'
            )

        models = cfg.models
        seeds = cfg.seeds
        snapshots = cfg.snapshots
        sources = cfg.sources
        if cfg.vars is None:
            vars_dict: Dict[str, Any] = {}
        else:
            vars_dict = cfg.vars
        vars_value = VarProvider(vars_dict)

        on_run_start: List[str] = value_or(cfg.on_run_start, [])
        on_run_end: List[str] = value_or(cfg.on_run_end, [])

        query_comment = _query_comment_from_cfg(cfg.query_comment)

        try:
            packages = package_config_from_data(packages_dict)
        except ValidationError as e:
            raise DbtProjectError(validator_error_message(e)) from e

        try:
            selectors = selector_config_from_data(selectors_dict)
        except ValidationError as e:
            raise DbtProjectError(validator_error_message(e)) from e

        project = cls(
            project_name=name,
            version=version,
            project_root=project_root,
            profile_name=profile_name,
            source_paths=source_paths,
            macro_paths=macro_paths,
            data_paths=data_paths,
            test_paths=test_paths,
            analysis_paths=analysis_paths,
            docs_paths=docs_paths,
            asset_paths=asset_paths,
            target_path=target_path,
            snapshot_paths=snapshot_paths,
            clean_targets=clean_targets,
            log_path=log_path,
            modules_path=modules_path,
            quoting=quoting,
            models=models,
            on_run_start=on_run_start,
            on_run_end=on_run_end,
            seeds=seeds,
            snapshots=snapshots,
            dbt_version=dbt_version,
            packages=packages,
            selectors=selectors,
            query_comment=query_comment,
            sources=sources,
            vars=vars_value,
            config_version=cfg.config_version,
        )
        # sanity check - this means an internal issue
        project.validate()
        return project

    def __str__(self):
        cfg = self.to_project_config(with_packages=True)
        return str(cfg)

    def __eq__(self, other):
        if not (isinstance(other, self.__class__) and
                isinstance(self, other.__class__)):
            return False
        return self.to_project_config(with_packages=True) == \
            other.to_project_config(with_packages=True)

    def to_project_config(self, with_packages=False):
        """Return a dict representation of the config that could be written to
        disk with `yaml.safe_dump` to get this configuration.

        :param with_packages bool: If True, include the serialized packages
            file in the root.
        :returns dict: The serialized profile.
        """
        result = deepcopy({
            'name': self.project_name,
            'version': self.version,
            'project-root': self.project_root,
            'profile': self.profile_name,
            'source-paths': self.source_paths,
            'macro-paths': self.macro_paths,
            'data-paths': self.data_paths,
            'test-paths': self.test_paths,
            'analysis-paths': self.analysis_paths,
            'docs-paths': self.docs_paths,
            'asset-paths': self.asset_paths,
            'target-path': self.target_path,
            'snapshot-paths': self.snapshot_paths,
            'clean-targets': self.clean_targets,
            'log-path': self.log_path,
            'quoting': self.quoting,
            'models': self.models,
            'on-run-start': self.on_run_start,
            'on-run-end': self.on_run_end,
            'seeds': self.seeds,
            'snapshots': self.snapshots,
            'sources': self.sources,
            'vars': self.vars.to_dict(),
            'require-dbt-version': [
                v.to_version_string() for v in self.dbt_version
            ],
            'config-version': self.config_version,
        })
        if self.query_comment:
            result['query-comment'] = self.query_comment.to_dict()

        if with_packages:
            result.update(self.packages.to_dict())

        return result

    def validate(self):
        try:
            ProjectContract.from_dict(self.to_project_config())
        except ValidationError as e:
            raise DbtProjectError(validator_error_message(e)) from e

    @classmethod
    def _get_required_version(
        cls, rendered_project: Dict[str, Any], verify_version: bool = False
    ) -> List[VersionSpecifier]:
        dbt_raw_version: Union[List[str], str] = '>=0.0.0'
        required = rendered_project.get('require-dbt-version')
        if required is not None:
            dbt_raw_version = required

        try:
            dbt_version = _parse_versions(dbt_raw_version)
        except SemverException as e:
            raise DbtProjectError(str(e)) from e

        if verify_version:
            # no name is also an error that we want to raise
            if 'name' not in rendered_project:
                raise DbtProjectError(
                    'Required "name" field not present in project',
                )
            validate_version(dbt_version, rendered_project['name'])

        return dbt_version

    @classmethod
    def render_from_dict(
        cls,
        project_root: str,
        project_dict: Dict[str, Any],
        packages_dict: Dict[str, Any],
        selectors_dict: Dict[str, Any],
        renderer: DbtProjectYamlRenderer,
        *,
        verify_version: bool = False
    ) -> 'Project':
        rendered_project = renderer.render_data(project_dict)
        rendered_project['project-root'] = project_root
        package_renderer = renderer.get_package_renderer()
        rendered_packages = package_renderer.render_data(packages_dict)
        selectors_renderer = renderer.get_selector_renderer()
        rendered_selectors = selectors_renderer.render_data(selectors_dict)

        try:
            dbt_version = cls._get_required_version(
                rendered_project, verify_version=verify_version
            )

            return cls.from_project_config(
                rendered_project,
                rendered_packages,
                rendered_selectors,
                dbt_version,
            )
        except DbtProjectError as exc:
            if exc.path is None:
                exc.path = os.path.join(project_root, 'dbt_project.yml')
            raise

    @classmethod
    def partial_load(
        cls, project_root: str, *, verify_version: bool = False
    ) -> PartialProject:
        project_root = os.path.normpath(project_root)
        project_dict = _raw_project_from(project_root)

        project_name = project_dict.get('name')
        profile_name = project_dict.get('profile')
        config_version = project_dict.get('config-version', 1)
        if config_version != 2:
            raise DbtProjectError(
                f'Invalid config version: {config_version}, expected 2',
                path=os.path.join(project_root, 'dbt_project.yml')
            )

        return PartialProject(
            config_version=config_version,
            profile_name=profile_name,
            project_name=project_name,
            project_root=project_root,
            project_dict=project_dict,
            verify_version=verify_version,
        )

    @classmethod
    def from_project_root(
        cls,
        project_root: str,
        renderer: DbtProjectYamlRenderer,
        *,
        verify_version: bool = False,
    ) -> 'Project':
        partial = cls.partial_load(project_root, verify_version=verify_version)
        renderer.version = partial.config_version
        return partial.render(renderer)

    def hashed_name(self):
        return hashlib.md5(self.project_name.encode('utf-8')).hexdigest()

    def get_selector(self, name: str) -> SelectionSpec:
        if name not in self.selectors:
            raise RuntimeException(
                f'Could not find selector named {name}, expected one of '
                f'{list(self.selectors)}'
            )
        return self.selectors[name]
