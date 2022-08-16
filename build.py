from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import shutil
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Deque, Dict, List, Optional

import cue
from whatrecord.makefile import Dependency, DependencyGroup, Makefile

logger = logging.getLogger(__name__)

MODULE_PATH = pathlib.Path(__file__).parent.resolve()


@dataclass
class VersionInfo:
    name: str
    base: str
    tag: str

    _module_path_regexes_: ClassVar[List[re.Pattern]] = [
        re.compile(
            base_path + "/"
            r"(?P<base>[^/]+)/"
            r"modules/"
            r"(?P<name>[^/]+)/"
            r"(?P<tag>[^/]+)/?"
        )
        for base_path in ("/cds/group/pcds/epics", "/reg/g/pcds/epics")
    ]

    @classmethod
    def from_path(cls, path: pathlib.Path) -> Optional[VersionInfo]:
        path_str = str(path.resolve())
        # TODO some sort of configuration
        for regex in cls._module_path_regexes_:
            match = regex.match(path_str)
            if match is None:
                continue
            return cls(**match.groupdict())
        return None

    def to_cue(self, variable_name: str) -> Dict[str, Any]:
        res = {
            "": self.tag or "master",
            "_DIRNAME": self.name,
            "_REPONAME": self.name,
            "_REPOOWNER": cue.setup.get("REPOOWNER", "slac-epics"),
            "_VARNAME": variable_name,
            "_RECURSIVE": "YES",
            "_DEPTH": -1,
        }
        res["_REPOURL"] = "https://github.com/{_REPOOWNER}/{_REPONAME}.git".format(
            **res
        )
        return {
            f"{variable_name}{key}": value
            for key, value in res.items()
        }


@contextlib.contextmanager
def monkeypatch(obj: object, attr: str, value: Any) -> Any:
    sentinel = object()

    old_value = getattr(obj, attr, sentinel)
    try:
        setattr(obj, attr, value)
        yield old_value
    finally:
        if old_value is not sentinel:
            setattr(obj, attr, old_value)
        else:
            delattr(obj, attr)


class CueShim:
    """
    A shim around epics-cue so I can keep it in one place and refactor if need
    be.
    """

    cache_path: pathlib.Path
    group: DependencyGroup

    def __init__(
        self,
        target_path: pathlib.Path,
        epics_base_for_introspection: pathlib.Path,
        cache_path: pathlib.Path = MODULE_PATH / "cache",
    ):
        self.cache_path = cache_path
        self.module_cache_path = cache_path / "modules"
        os.environ["CACHEDIR"] = str(self.module_cache_path)
        import cue
        self._cue = cue
        self._cue.prepare_env()
        self._cue.detect_context()
        self.dependency_by_variable = {}
        self.epics_base_for_introspection = epics_base_for_introspection.resolve()
        self.group = self._set_primary_target(target_path)

    def _set_primary_target(self, path: pathlib.Path) -> DependencyGroup:
        # TODO: RELEASE_SITE may need to be generated if unavailable; 
        # see eco-tools
        release_site = path / "RELEASE_SITE"
        if release_site.exists():
            shutil.copy(release_site, self.cache_path)
        makefile = self.get_makefile_for_path(path)
        return DependencyGroup.from_makefile(makefile)

    def get_makefile_for_path(self, path: pathlib.Path) -> Makefile:
        return Makefile.from_file(
            Makefile.find_makefile(path),
            keep_os_env=False,
            # NOTE: may need to specify an existing epics-base to get the build
            # system makefiles.  Alternatively, a barebones version could be
            # packaged to do so?
            variables=dict(EPICS_BASE=str(self.epics_base_for_introspection)),
        )

    def get_path_for_dependency(self, dep: VersionInfo) -> pathlib.Path:
        return self.module_cache_path / f"{dep.name}-{dep.tag}"

    def update_settings(self, settings: Dict[str, str], overwrite: bool = True):
        for key, value in settings.items():
            old_value = self._cue.setup.get(key, None)
            if old_value == value:
                continue
            if old_value is not None:
                if overwrite:
                    logger.debug("cue setup overwriting %s: old=%r new=%r", key, old_value, value)
                    self._cue.setup[key] = value
                else:
                    logger.debug("cue setup not overwriting: %s=%r", key, old_value)
            else:
                logger.debug("cue setup %s=%r", key, value)
                self._cue.setup[key] = value

    def add_dependency(self, variable_name: str, version: VersionInfo) -> Dependency:
        logger.info("Updating cue settings for dependency %s: %s", variable_name, version)
        self.update_settings(version.to_cue(variable_name), overwrite=True)

        def no_op(*args, **kwargs):
            ...

        # Tell cue to clone it, but make sure we keep our RELEASE settings
        # as-is
        with monkeypatch(self._cue, "update_release_local", no_op):
            self._cue.add_dependency(variable_name)

        cache_path = self.get_path_for_dependency(version)
        makefile = self.get_makefile_for_path(cache_path)
        dep = Dependency.from_makefile(
            makefile,
            recurse=True,
            name=version.name,
            variable_name=variable_name,
            root=self.group,
        )
        self.dependency_by_variable[variable_name] = dep

        return dep
        

def test():
    # get missing dependencies cloned into the cache
    checked = set()

    introspection_base = pathlib.Path("/Users/klauer/Repos/epics-base")
    cue_shim = CueShim(pathlib.Path("ads-ioc"), introspection_base)

    def done() -> bool:
        return all(
            dep.variable_name in checked 
            for dep in cue_shim.group.all_modules.values()
        )

    while not done():
        deps = list(cue_shim.group.all_modules.values())
        for dep in deps:
            if dep.variable_name in checked:
                continue
            checked.add(dep.variable_name)

            for var, path in dep.missing_paths.items():
                if var in checked:
                    if var not in cue_shim.dependency_by_variable:
                        logger.warning("Dependency still missing; %s", var)
                    continue

                version_info = VersionInfo.from_path(path)
                if version_info is None:
                    logger.debug(
                        "Dependency path for %r=%r does not match known patterns", var, path
                    )
                    continue

                cue_shim.add_dependency(var, version_info)


if __name__ == "__main__":
    logging.basicConfig(level="WARNING")
    logger.setLevel("DEBUG")
    test()
