"""Models representing execution context of Copier."""
from contextlib import suppress
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any, Callable, ChainMap, ChainMap as t_ChainMap, Literal, Optional
from unicodedata import normalize

import pathspec
import yaml
from jinja2.sandbox import SandboxedEnvironment
from plumbum.cmd import git
from plumbum.machines import local
from pydantic import BaseModel
from pydantic.class_validators import validator
from pydantic.fields import Field, PrivateAttr

from copier.config.factory import filter_config, verify_minimum_version
from copier.config.objects import DEFAULT_DATA, DEFAULT_EXCLUDE, ConfigData
from copier.config.user_data import Question, Questionary, load_config_data
from copier.tools import get_jinja_env
from copier.types import AnyByStrDict, OptStr, StrSeq

from .vcs import clone, get_repo, is_git_repo_root


def lazy(method):
    @wraps(method)
    def _wrapper(self, *args, **kwargs):
        cache_key = f"_{method.__name__}"
        while True:
            with suppress(KeyError):
                return self.__slots__[cache_key]
            self.__slots__[cache_key] = method(*args, **kwargs)

    return method


class AnswersMap(BaseModel):
    init: AnyByStrDict = Field(default_factory=dict)
    user: AnyByStrDict = Field(default_factory=dict)
    last: AnyByStrDict = Field(default_factory=dict)
    default: AnyByStrDict = Field(default_factory=dict)

    # Private
    _local: AnyByStrDict = PrivateAttr(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True

    @validator(
        "init",
        "user",
        "last",
        "default",
        allow_reuse=True,
        pre=True,
        each_item=True,
    )
    def _deep_copy_answers(cls, v: AnyByStrDict) -> AnyByStrDict:
        """Make sure all dicts are copied."""
        return deepcopy(v)

    @lazy
    def combined(self) -> t_ChainMap[str, Any]:
        """Answers combined from different sources, sorted by priority."""
        return ChainMap(
            self._local,
            self.user,
            self.init,
            self.last,
            self.default,
            DEFAULT_DATA,
        )


class Template(BaseModel):
    url: str
    ref: OptStr

    class Config:
        arbitrary_types_allowed = True

    @lazy
    def _raw_config(self) -> AnyByStrDict:
        result = load_config_data(self.local_path())
        with suppress(KeyError):
            verify_minimum_version(result["_min_copier_version"])
        return result

    @lazy
    def default_answers(self) -> AnyByStrDict:
        return {
            key: value.get("default") for key, value in self.questions_data().items()
        }

    @lazy
    def config_data(self) -> AnyByStrDict:
        return filter_config(self._raw_config())[0]

    @lazy
    def questions_data(self) -> AnyByStrDict:
        return filter_config(self._raw_config())[1]

    @lazy
    def local_path(self) -> Path:
        if self.vcs() == "git" and not is_git_repo_root(self.url_expanded()):
            return Path(clone(self.url_expanded(), self.ref))
        return Path(self.url)

    @lazy
    def url_expanded(self) -> str:
        return get_repo(self.url) or self.url

    @lazy
    def vcs(self) -> Optional[Literal["git"]]:
        if get_repo(self.url):
            return "git"


class Subproject(BaseModel):
    local_path: Path
    answers_relpath: Path = Path(".copier-answers.yml")

    class Config:
        arbitrary_types_allowed = True

    def is_dirty(self) -> bool:
        if self.vcs() == "git":
            with local.cwd(self.local_path):
                return bool(git("status", "--porcelain").strip())
        return False

    @lazy
    def _raw_answers(self) -> AnyByStrDict:
        try:
            return yaml.safe_load((self.local_path / self.answers_relpath).read_text())
        except OSError:
            return {}

    @lazy
    def last_answers(self) -> AnyByStrDict:
        return {
            key: value
            for key, value in self._raw_answers()
            if key in {"_src_path", "_commit"} or not key.startswith("_")
        }

    @lazy
    def template(self) -> Optional[Template]:
        last_url = self._raw_answers().get("_src_path")
        last_ref = self._raw_answers().get("_commit")
        if last_url:
            return Template(url=last_url, ref=last_ref)

    @lazy
    def vcs(self) -> Optional[Literal["git"]]:
        if is_git_repo_root(self.local_path):
            return "git"


class Copier(BaseModel):
    conf: ConfigData  # TODO Remove
    exclude: StrSeq = ()

    class Config:
        arbitrary_types_allowed = True

    def _path_matcher(self, patterns: StrSeq) -> Callable[[Path], bool]:
        # TODO Is normalization really needed?
        normalized_patterns = map(normalize, ("NFD",), patterns)
        spec = pathspec.PathSpec.from_lines("gitwildmatch", normalized_patterns)
        return spec.match_file

    @lazy
    def answers(self) -> AnswersMap:
        return AnswersMap(
            init=self.conf.data_from_init,
            last=self.subproject().last_answers(),
            default=self.template().default_answers(),
        )

    @lazy
    def conf_patched(self) -> ConfigData:
        return self.conf.copy(
            update={
                "commit": self.answers().last.get("_commit"),
                "original_src_path": str(self.conf.src_path),
            }
        )

    @lazy
    def all_exclusions(self) -> StrSeq:
        base = self.template().config_data().get("exclude", DEFAULT_EXCLUDE)
        return tuple(base) + tuple(self.exclude)

    @lazy
    def jinja_env(self) -> SandboxedEnvironment:
        return get_jinja_env(self.conf.envops)

    @lazy
    def questionary(self) -> Questionary:
        result = Questionary(
            answers_default=self.answers().default,
            answers_forced=self.answers().init,
            answers_last=self.answers().last,
            answers_user=self.answers().user,
            ask_user=not self.conf.force,
            env=self.jinja_env(),
        )
        for question, details in self.template().questions_data().items():
            # TODO Append explicitly?
            Question(var_name=question, questionary=result, **details)
        return result

    @lazy
    def subproject(self) -> Subproject:
        return Subproject(
            local_path=self.conf.dst_path, answers_relpath=self.conf.answers_file
        )

    @lazy
    def template(self) -> Template:
        try:
            return Template(url=str(self.conf.src_path), ref=self.conf.vcs_ref)
        except TypeError:
            return self.subproject().template()

    # Main operations
    def run_auto(self) -> None:
        if self.conf.src_path:
            return self.run_copy()
        return self.run_update()

    def run_copy(self) -> None:
        """Generate a subproject from zero, ignoring what was in the folder."""
        must_exclude = self._path_matcher(self.all_exclusions())

        render = Renderer(conf)
        skip_patterns = [render.string(pattern) for pattern in conf.skip_if_exists]
        must_skip = create_path_filter(skip_patterns)

        if not conf.quiet:
            print("")  # padding space

        folder: StrOrPath
        rel_folder: StrOrPath

        src_path = conf.src_path
        if conf.subdirectory is not None:
            src_path /= conf.subdirectory

        for folder, sub_dirs, files in os.walk(src_path):
            rel_folder = str(folder).replace(str(src_path), "", 1).lstrip(os.path.sep)
            rel_folder = render.string(rel_folder)
            rel_folder = str(rel_folder).replace("." + os.path.sep, ".", 1)

            if must_exclude(rel_folder):
                # Folder is excluded, so stop walking it
                sub_dirs[:] = []
                continue

            folder = Path(folder)
            rel_folder = Path(rel_folder)

            render_folder(rel_folder, conf)

            source_paths = get_source_paths(
                conf, folder, rel_folder, files, render, must_exclude
            )
            for source_path, rel_path in source_paths:
                render_file(conf, rel_path, source_path, render, must_skip)

        if not conf.quiet:
            print("")  # padding space

        run_tasks(
            conf,
            render,
            [{"task": t, "extra_env": {"STAGE": "task"}} for t in conf.tasks],
        )
        if not conf.quiet:
            print("")  # padding space

    def run_update(self) -> None:
        from copier.main import update_diff

        return update_diff(self.conf_patched())
