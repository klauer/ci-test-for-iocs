#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

import cue
from whatrecord.makefile import Dependency, DependencyGroup, Makefile

logger = logging.getLogger(__name__)

MODULE_PATH = pathlib.Path(__file__).parent.resolve()


# TODO: config file of sorts, or this becomes part of the SLAC-specific build
# system
repo_owner_overrides = {
}

repo_name_overrides = {
    "base": "epics-base",
}

# When writing out cue .set files, use these prefixes instead of the build
# variable names:
cue_set_name_overrides = {
    "EPICS_BASE": "BASE",
}


@dataclass
class PcdsBuildPaths:
    epics_base: pathlib.Path
    epics_site_top: pathlib.Path
    epics_modules: pathlib.Path

    def to_variables(self) -> Dict[str, str]:
        return {
            var.upper(): str(value.resolve())
            for var, value in dataclasses.asdict(self).items()
        }


@dataclass
class CueOptions:
    # For all commands:

    #: Assume vcvarsall.bat has already been run
    no_vcvars: bool = False
    #: Append directory to $PATH or %%PATH%%.  Expands {ENVVAR}
    paths: List[str] = field(default_factory=list)
    #: Terminate make after delay, in seconds.
    timeout: int = 10000
    # for build:
    makeargs: List[str] = field(default_factory=list)
    # for exec:
    cmd: List[str] = field(default_factory=list)


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
        prefix_name = variable_name
        default_owner = cue.setup.get("REPOOWNER", "slac-epics")
        res = {
            "": self.tag or "master",
            "_DIRNAME": self.name,
            "_REPONAME": repo_name_overrides.get(self.name, self.name),
            "_REPOOWNER": repo_owner_overrides.get(default_owner, default_owner),
            "_VARNAME": variable_name,  # for RELEASE.local
            "_RECURSIVE": "YES",
            "_DEPTH": "-1",
        }
        res["_REPOURL"] = "https://github.com/{_REPOOWNER}/{_REPONAME}.git".format(
            **res
        )
        return {
            f"{prefix_name}{key}": value
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

    #: Cache path where all dependencies go.
    cache_path: pathlib.Path
    #: whatrecord dependency information keyed by build variable name.
    dependency_by_variable: Dict[str, Dependency]
    #: epics-base to use in the initial setup stage with whatrecord, required
    #: for the GNU make-based build system
    introspection_paths: PcdsBuildPaths
    #: The top-level whatrecord dependency group which gets updated as we
    #: check out more dependencies.
    group: DependencyGroup
    #: The subdirectory of the cache path where modules are stored.  Kept this
    #: way for SLAC EPICS to have RELEASE_SITE there (TODO)
    module_cache_path: pathlib.Path
    #: The default repository organization for modules.
    repo_owner: str
    #: Where generated cue.py 'set' files are to be stored.
    set_path: pathlib.Path
    #: Version information by variable name, derived from whatrecord-provided
    #: makefile introspection.
    version_by_variable: Dict[str, VersionInfo]

    def __init__(
        self,
        target_path: pathlib.Path,
        introspection_paths: PcdsBuildPaths,
        set_path: pathlib.Path = MODULE_PATH / "cache" / "sets",
        cache_path: pathlib.Path = MODULE_PATH / "cache",
        local: bool = False,
        github_org: str = "slac-epics",
    ):
        self.cache_path = cache_path
        self.module_cache_path = cache_path / "modules"
        self.dependency_by_variable = {}
        self.version_by_variable = {}
        self.introspection_paths = introspection_paths
        self.group = self._set_primary_target(target_path)
        self.set_path = set_path
        self.local = local
        self.github_org = github_org
        self._import_cue()

    def _import_cue(self):
        """This is ugly, I know.  Treat 'cue.py' as a class of sorts."""
        import cue  # noqa
        os.environ["CACHEDIR"] = str(self.module_cache_path)
        os.environ["SETUP_PATH"] = str(self.set_path)
        if self.local:
            # Pretend we're github actions for now
            os.environ["GITHUB_ACTIONS"] = "1"
            if sys.platform == "darwin":
                os.environ["RUNNER_OS"] = "macOS"
                # os.environ["CMP"] = "clang"
                # Try homebrew-installed gcc:
                os.environ["CMP"] = "gcc-12"
            else:
                # untested
                os.environ["RUNNER_OS"] = "Linux"
                # os.environ["CMP"] = "gcc-4.9"
                os.environ["CMP"] = "gcc"

        self._cue = cue
        self._cue.setup["REPOOWNER"] = self.github_org
        self._cue.prepare_env()
        self._cue.detect_context()

        # We want to build our dependencies because they are source
        # distributions only.
        self._cue.skip_dep_builds = False
        # Force a recompilation step, no matter what cue says.
        self._cue.do_recompile = True  # TODO
        self._patch_cue()

    def _patch_cue(self):
        # 1. Patch `call_git` to insert `--template` in git clone, allowing
        # us to intercept invalid AFS submodules

        def call_git(args: List[str], **kwargs):
            if args and args[0] == "clone":
                git_template_path = MODULE_PATH / "git-template"
                args.insert(1, f"--template={git_template_path}")
            return orig_call_git(args, **kwargs)

        orig_call_git = self._cue.call_git
        self._cue.call_git = call_git

    def get_build_order(self) -> List[str]:
        """Get the build order by variable name."""
        # TODO: order based on dependency graph could/should be done efficiently
        build_order = ["EPICS_BASE"]
        skip = []
        remaining = set(self.version_by_variable) - set(build_order) - set(skip)
        last_remaining = None
        remaining_requires = {
            dep: list(
                var
                for var in self.dependency_by_variable[dep].dependencies
                if var != dep
            )
            for dep in remaining
        }
        logger.debug(
            "Trying to determine build order based on these requirements: %s",
            remaining_requires
        )
        while remaining:
            for to_check_name in sorted(remaining):
                dep = self.dependency_by_variable[to_check_name]
                if all(subdep in build_order for subdep in dep.dependencies):
                    build_order.append(to_check_name)
                    remaining.remove(to_check_name)
            if last_remaining == remaining:
                remaining_requires = {
                    dep: list(self.dependency_by_variable[dep].dependencies)
                    for dep in remaining
                }
                logger.warning(
                    f"Unable to determine build order.  Determined build order:\n"
                    f"{build_order}\n"
                    f"\n"
                    f"Remaining:\n"
                    f"{remaining}\n"
                    f"\n"
                    f"which require:\n"
                    f"{remaining_requires}"
                )
                for remaining_dep in remaining:
                    build_order.append(remaining_dep)
                break

            last_remaining = set(remaining)

        # EPICS_BASE is implicit: added in cue.modlist() automatically
        return build_order[1:]

    def create_set_text(self):
        result = []
        for variable in ["EPICS_BASE"] + self.get_build_order():
            version = self.version_by_variable[variable]
            cue_set_name = cue_set_name_overrides.get(variable, variable)
            for key, value in version.to_cue(cue_set_name).items():
                result.append(f"{key}={value}")
        return "\n".join(result)

    def write_set_to_file(self, name: str) -> pathlib.Path:
        self.set_path.mkdir(parents=True, exist_ok=True)
        set_filename = self.set_path / f"{name}.set"
        with open(set_filename, "wt") as fp:
            print(self.create_set_text(), file=fp)
        return set_filename

    def _set_primary_target(self, path: pathlib.Path) -> DependencyGroup:
        # TODO: RELEASE_SITE may need to be generated if unavailable;
        # see eco-tools
        # release_site = path / "RELEASE_SITE"
        # if release_site.exists():
        #     shutil.copy(release_site, self.cache_path)
        makefile = self.get_makefile_for_path(path)
        return DependencyGroup.from_makefile(makefile)

    def get_makefile_for_path(self, path: pathlib.Path) -> Makefile:
        return Makefile.from_file(
            Makefile.find_makefile(path),
            keep_os_env=False,
            # NOTE: may need to specify an existing epics-base to get the build
            # system makefiles.  Alternatively, a barebones version could be
            # packaged to do so?
            variables=self.introspection_paths.to_variables(),
        )

    def get_path_for_version_info(self, dep: VersionInfo) -> pathlib.Path:
        tag = dep.tag
        if "-branch" in tag:
            tag = tag.replace("-branch", "")
        return self.module_cache_path / f"{dep.name}-{tag}"

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

    def git_reset_repo_directory(self, variable_name: str, directory: str):
        version = self.version_by_variable[variable_name]
        module_path = self.get_path_for_version_info(version)
        self._cue.call_git(["checkout", "--", directory], cwd=str(module_path))

    def add_dependency(self, variable_name: str, version: VersionInfo) -> Dependency:
        cue_variable_name = cue_set_name_overrides.get(variable_name, variable_name)
        logger.info("Updating cue settings for dependency %s: %s", variable_name, version)
        self.update_settings(version.to_cue(cue_variable_name), overwrite=True)
        self.version_by_variable[variable_name] = version

        self._cue.add_dependency(cue_variable_name)

        self.git_reset_repo_directory(variable_name, "configure")
        cache_path = self.get_path_for_version_info(version)
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

    @property
    def module_release_local(self) -> pathlib.Path:
        return self.module_cache_path / "RELEASE.local"

    def find_all_dependencies(self):
        """
        Using module path conventions, find all dependencies and check them
        out to the cache directory.

        See Also
        --------
        :func:`VersionInfo.from_path`
        """
        checked = set()

        # see note below about dependency ordering...
        self.module_release_local.unlink(missing_ok=True)

        def done() -> bool:
            return all(
                dep.variable_name in checked
                for dep in self.group.all_modules.values()
            )

        while not done():
            deps = list(self.group.all_modules.values())
            for dep in deps:
                if dep.variable_name in checked:
                    continue
                checked.add(dep.variable_name)

                logger.debug(
                    "Checking module for dependencies: %s. "
                    "Existing dependencies: %s Missing paths: %s",
                    dep.variable_name,
                    ", ".join(f"{var}={value}" for var, value in dep.dependencies.items()),
                    ", ".join(f"{var}={value}" for var, value in dep.missing_paths.items()),
                )

                for var, path in dep.missing_paths.items():
                    if var in checked:
                        version = self.version_by_variable.get(var, None)
                        if version is not None:
                            dep.dependencies[var] = self.get_path_for_version_info(version)
                        else:
                            logger.warning("Dependency still missing; %s", var)
                        continue

                    version_info = VersionInfo.from_path(path)
                    if version_info is None:
                        logger.debug(
                            "Dependency path for %r=%r does not match known patterns", var, path
                        )
                        continue

                    self.add_dependency(var, version_info)
                    dep.dependencies[var] = self.get_path_for_version_info(version_info)
                    logger.info(
                        "Set dependency of %s: %s=%s",
                        dep.variable_name or "the IOC",
                        var,
                        dep.dependencies[var],
                    )

    def use_epics_base(self, tag: str):
        # "building base" means that the ci script is used _just_ for epics-base
        # and is located in the current working directory (".").  Don't set it
        # for our modules/IOCs.
        self._cue.building_base = False
        base_version = VersionInfo(
            name="epics-base",
            base=tag,
            tag=tag,
        )
        cache_base = self.cache_path / "base"
        cache_base.mkdir(parents=True, exist_ok=True)

        with open(self.cache_path / "RELEASE_SITE", "wt") as fp:
            print("EPICS_SITE_TOP=", file=fp)
            print(f"BASE_MODULE_VERSION={tag}", file=fp)
            print("EPICS_MODULES=$(EPICS_SITE_TOP)/modules", file=fp)

        tagged_base_path = self.cache_path / "base" / tag
        if tagged_base_path.exists() and tagged_base_path.is_symlink():
            tagged_base_path.unlink()

        os.symlink(
            # modules/epics-base-... ->
            self.get_path_for_version_info(base_version),
            # base/tag/...
            tagged_base_path
        )
        self.add_dependency("EPICS_BASE", base_version)

    def update_release_local(self):
        for dep in self.dependency_by_variable.values():
            assert dep.variable_name is not None
            version = self.version_by_variable[dep.variable_name]
            dep_path = self.get_path_for_version_info(version)
            logger.debug("Updating RELEASE.local: %s=%s", dep.variable_name, dep_path)
            self._cue.update_release_local(dep.variable_name, str(dep_path))

            if dep in ("EPICS_BASE", "BASE"):  # argh, why can't I remember which
                continue

            for makefile_relative in dep.makefile.makefile_list:
                makefile = (dep_path / makefile_relative).resolve()
                try:
                    makefile.relative_to(dep_path)
                except ValueError:
                    logger.warning(
                        "Skipping makefile: %s (not relative to %s)", makefile, dep_path
                    )
                else:
                    try:
                        self.patch_makefile(dep, makefile)
                    except PermissionError:
                        logger.error("Failed to patch makefile due to permissions: %s", makefile)
                    except Exception:
                        logger.exception("Failed to patch makefile: %s", makefile)

    def patch_makefile(self, dep: str, makefile: pathlib.Path):
        to_update = {
            var: self.get_path_for_version_info(version)
            for var, version in self.version_by_variable.items()
        }

        def fix_line(line: str) -> str:
            if not line:
                return line
            if line[0] in " \t#":
                return line

            if "=" in line:
                line = line.rstrip()
                var, _ = line.split("=", 1)
                var = var.strip()
                if var in to_update:
                    fixed = f"{var}={to_update[var]}"
                    logger.debug("Fixed %s Makefile line: %s", dep, fixed)
                    return fixed
            return line

        with open(makefile, "rt") as fp:
            lines = fp.read().splitlines()

        output_lines = [fix_line(line) for line in lines]
        if lines != output_lines:
            logger.warning("Patching makefile: %s", makefile)
            with open(makefile, "wt") as fp:
                print("\n".join(output_lines), file=fp)
        else:
            logger.debug("Makefile left unchanged: %s", makefile)

    def update_build_order(self) -> List[str]:
        build_order = self.get_build_order()
        logger.debug(
            "Determined build order of modules for cue: %s",
            ", ".join(build_order),
        )
        self._cue.modules_to_compile[:] = [
            cue_set_name_overrides.get(variable, variable)
            for variable in build_order
        ]
        return build_order


def main(ioc_path: str):
    # local_base = pathlib.Path("/Users/klauer/Repos/epics-base")
    # if local_base.exists():
    #     introspection_base = local_base

    cue_shim = CueShim(
        target_path=pathlib.Path(ioc_path).resolve(),
        # For introspection
        introspection_paths=PcdsBuildPaths(
            epics_base=pathlib.Path("/cds/group/pcds/epics/base/R7.0.2-2.0/"),
            epics_site_top=pathlib.Path("/cds/group/pcds/epics/"),
            epics_modules=pathlib.Path("/cds/group/pcds/epics/R7.0.2-2.0/modules"),
        ),
        local=True,
    )
    # NOTE/TODO: use the 7.0.3.1-2.0 *branch* for noW:
    # R7.0.3.1-2.0 is a branch, whereas R7.0.3.1-2.0.1 is a tag;
    cue_shim.use_epics_base("R7.0.2-2.branch")
    # /cds/group/pcds/epics/base/R7.0.3.1-2.0 is where all minor local fixes
    # go for 7.0.3.1-2.0.
    # cue_shim.use_epics_base("R7.0.3.1-2.0-branch")
    cue_shim.find_all_dependencies()
    cue_shim.write_set_to_file("defaults")
    cue_shim.update_release_local()
    cue_shim.update_build_order()
    # TODO: slac-epics/epics-base has absolute /afs submodule paths :(
    cue_shim._cue.prepare(CueOptions())
    cue_shim._cue.build(CueOptions())


if __name__ == "__main__":
    logging.basicConfig(level="WARNING")
    logger.setLevel("DEBUG")
    main(ioc_path=sys.argv[1])
