#!/usr/bin/env python3
from __future__ import annotations

import dataclasses
import functools
import logging
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, List, Optional, Set

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

is_local = "GITHUB_ACTIONS" in os.environ


def folded_output(title: str = ""):
    """
    Fold the function output in GitHub actions logs.

    Wrapper for a method or other function.

    Parameters
    ----------
    title : str, optional
        The title to show. Defaults to the function name.
    """

    def wrapper(func: Callable) -> Callable:
        if is_local:
            return func

        nonlocal title

        if not title:
            title = func.__name__

        tag = re.sub("[^0-9a-zA-Z_]+", ".", title)

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            with cue.Folded(tag, title):
                return func(*args, **kwargs)
        return wrapped
    return wrapper


@dataclass
class PcdsBuildPaths:
    #: Path to epics-base.
    epics_base: pathlib.Path = pathlib.Path("/cds/group/pcds/epics/base/R7.0.2-2.0")
    #: Path to where epics-base and modules are contained.
    epics_site_top: pathlib.Path = pathlib.Path("/cds/group/pcds/")
    #: Path to where this specific epics-base has its modules.
    epics_modules: pathlib.Path = pathlib.Path("/cds/group/pcds/epics/R7.0.2-2.0/modules")

    def to_variables(self) -> Dict[str, str]:
        """
        Generate a dictionary of variable-to-string-value.

        Returns
        -------
        Dict[str, str]
            ```{"EPICS_BASE": "/path/to/epics-base/", ...}```
        """
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


def patch_makefile(makefile: pathlib.Path, variables: Dict[str, Any]) -> Set[str]:
    """
    Patch Makefile variable declarations with those provided in ``variables``.

    Parameters
    ----------
    makefile : pathlib.Path
        Path to the Makefile.
    variables : Dict[str, Any]
        Variable-to-value dictionary.

    Returns
    -------
    Set[str]
        Set of updated variables.
    """
    updated = set()

    def fix_line(line: str) -> str:
        if not line:
            return line
        if line[0] in " \t#":
            return line

        for separator in ("?=", ":=", "="):
            if separator in line:
                line = line.rstrip()
                var, _ = line.split(separator, 1)
                var = var.strip()
                if var in variables:
                    fixed = f"{var}{separator}{variables[var]}"
                    updated.add(var)
                    return fixed

        return line

    with open(makefile, "rt") as fp:
        lines = fp.read().splitlines()

    output_lines = [fix_line(line) for line in lines]
    if updated:
        logger.warning(
            "Patching makefile %s variables %s", makefile, ", ".join(updated)
        )
        with open(makefile, "wt") as fp:
            print("\n".join(output_lines), file=fp)
    else:
        logger.debug("Makefile left unchanged: %s", makefile)
    return updated


class CueShim:
    """
    A shim around epics-cue so I can keep it in one place and refactor if need
    be.
    """

    #: Cache path where all dependencies go.
    cache_path: pathlib.Path
    #: whatrecord dependency information keyed by build variable name.
    variable_to_dependency: Dict[str, Dependency]
    #: epics-base to use in the initial setup stage with whatrecord, required
    #: for the GNU make-based build system
    introspection_paths: PcdsBuildPaths
    #: The top-level whatrecord dependency group which gets updated as we
    #: check out more dependencies.
    group: Optional[DependencyGroup]
    #: The subdirectory of the cache path where modules are stored.  Kept this
    #: way for SLAC EPICS to have RELEASE_SITE there (TODO)
    module_cache_path: pathlib.Path
    #: The default repository organization for modules.
    repo_owner: str
    #: Where generated cue.py 'set' files are to be stored.
    set_path: pathlib.Path
    #: Version information by variable name, derived from whatrecord-provided
    #: makefile introspection.
    variable_to_version: Dict[str, VersionInfo]

    def __init__(
        self,
        target_path: pathlib.Path,
        set_path: pathlib.Path = MODULE_PATH / "cache" / "sets",
        cache_path: pathlib.Path = MODULE_PATH / "cache",
        local: bool = False,
        github_org: str = "slac-epics",
    ):
        self.cache_path = cache_path
        self.module_cache_path = cache_path / "modules"
        self.variable_to_dependency = {}
        self.variable_to_version = {}
        self.introspection_paths = PcdsBuildPaths()
        self.target_path = target_path
        self.group = None
        self.set_path = set_path
        self.local = local
        self.github_org = github_org
        self._import_cue()

    def _import_cue(self):
        """This is ugly, I know.  Treat 'cue.py' as a class of sorts."""
        import cue  # noqa
        os.environ["CACHEDIR"] = str(self.module_cache_path)
        os.environ["SETUP_PATH"] = str(self.set_path)
        # TODO: deps should be cleaned normally
        os.environ["CLEAN_DEPS"] = "no"
        # TODO: some of our modules are not parallel-friendly
        os.environ["PARALLEL_MAKE"] = "0"
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

    def _patch_cue(self) -> None:
        """
        Monkeypatch cue to do what we expect:

        *. Patch `call_git` to insert `--template` in git clone, allowing
            us to intercept invalid AFS submodules
        """

        def call_git(args: List[str], **kwargs):
            if args and args[0] == "clone":
                git_template_path = MODULE_PATH / "git-template"
                args.insert(1, f"--template={git_template_path}")
            return orig_call_git(args, **kwargs)

        orig_call_git = self._cue.call_git
        self._cue.call_git = call_git

    def get_build_order(self) -> List[str]:
        """
        Get the build order by variable name.

        Returns
        -------
        list of str
            List of Makefile-defined variable names, in order of how they
            should be built.
        """
        # TODO: order based on dependency graph could/should be done efficiently
        build_order = ["EPICS_BASE"]
        skip = []
        remaining = set(self.variable_to_version) - set(build_order) - set(skip)
        last_remaining = None
        remaining_requires = {
            dep: list(
                var
                for var in self.variable_to_dependency[dep].dependencies
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
                dep = self.variable_to_dependency[to_check_name]
                if all(subdep in build_order for subdep in dep.dependencies):
                    build_order.append(to_check_name)
                    remaining.remove(to_check_name)
            if last_remaining == remaining:
                remaining_requires = {
                    dep: list(self.variable_to_dependency[dep].dependencies)
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

        logger.debug("Determined build order: %s", ", ".join(build_order))
        return build_order

    def create_set_text(self) -> str:
        """
        Generate the .set file contents based on the configured versions.

        Returns
        -------
        str
            The .set file contents.
        """
        result = []
        for variable in ["EPICS_BASE"] + self.get_build_order():
            version = self.variable_to_version[variable]
            cue_set_name = cue_set_name_overrides.get(variable, variable)
            for key, value in version.to_cue(cue_set_name).items():
                result.append(f"{key}={value}")
        return "\n".join(result)

    def write_set_to_file(self, name: str) -> pathlib.Path:
        """
        Write a cue .set file to the setup directory.

        Parameters
        ----------
        name : str
            The name of the .set file to write.

        Returns
        -------
        pathlib.Path
            The path to the file that was written.
        """
        self.set_path.mkdir(parents=True, exist_ok=True)
        set_filename = self.set_path / f"{name}.set"
        with open(set_filename, "wt") as fp:
            print(self.create_set_text(), file=fp)
        return set_filename

    def _create_dependency_group(self) -> DependencyGroup:
        """
        Set the primary target - IOC or module - path.

        Returns
        -------
        DependencyGroup
            The top-level `DependencyGroup` which is what whatrecord uses
            to track dependencies.
        """
        # TODO: RELEASE_SITE may need to be generated if unavailable;
        # see eco-tools
        # release_site = path / "RELEASE_SITE"
        # if release_site.exists():
        #     shutil.copy(release_site, self.cache_path)

        # Make sure any previous modifications don't change our introspection efforts:
        # TODO: instead, make the path matcher accept the cache directory, and
        # match {cache_path}/modules/{module}-{version}
        self.git_reset_repo_directory(None, "configure")

        makefile = self.get_makefile_for_path(self.target_path)
        return DependencyGroup.from_makefile(makefile)

    def get_makefile_for_path(self, path: pathlib.Path) -> Makefile:
        """
        Get a whatrecord :class:`Makefile` for the provided path.

        Parameters
        ----------
        path : pathlib.Path
            The path to search for a Makefile, or a path to the makefile
            itself.

        Returns
        -------
        Makefile
        """
        return Makefile.from_file(
            Makefile.find_makefile(path),
            keep_os_env=False,
            # NOTE: may need to specify an existing epics-base to get the build
            # system makefiles.  Alternatively, a barebones version could be
            # packaged to do so?
            variables=self.introspection_paths.to_variables(),
        )

    def get_path_for_version_info(self, dep: VersionInfo) -> pathlib.Path:
        """
        Get the cache path for the provided dependency with version
        information.

        Parameters
        ----------
        dep : VersionInfo
            The version information for the dependency, either derived by way
            of introspection or manually.

        Returns
        -------
        pathlib.Path

        """
        tag = dep.tag
        if "-branch" in tag:
            tag = tag.replace("-branch", "")
        return self.module_cache_path / f"{dep.name}-{tag}"

    def update_settings(self, settings: Dict[str, str], overwrite: bool = True):
        """
        Update cue's settings dictionary verbosely.

        Parameters
        ----------
        settings : Dict[str, str]
            Settings to update, key to value.
        overwrite : bool, optional
            Overwrite existing settings.  Defaults to True.
        """
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

    def git_reset_repo_directory(self, variable_name: Optional[str], directory: str):
        """
        Run git reset (or git checkout --) on a specific subdirectory
        of a dependency.

        Parameters
        ----------
        variable_name : str
            The dependency's variable name (as defined in makefiles).
        directory : str
            The subdirectory of that dependency.
        """
        if variable_name is None:
            module_path = self.target_path
        else:
            version = self.variable_to_version[variable_name]
            module_path = self.get_path_for_version_info(version)
        self._cue.call_git(["checkout", "--", directory], cwd=str(module_path))

    def add_dependency(
        self,
        variable_name: str,
        version: VersionInfo,
        add_to_group: bool = True,
        reset_configure: bool = True,
    ) -> Optional[Dependency]:
        """
        Add a dependency identified by its variable name and version tag.

        Parameters
        ----------
        variable_name : str
            The Makefile-defined variable for the dependency.
        version : VersionInfo
            The version information for the dependency, either derived by way
            of introspection or manually.
        add_to_group : bool, optional
            Use whatrecord to introspect the dependency's makefile and add it to the
            :class:`DependencyGroup` ``.group``.
        reset_configure : bool, optional
            Reset module/configure/* to the git HEAD, if changed.
            (TODO) also reset modules/RELEASE.local

        Returns
        -------
        Dependency
            The whatrecord-generated :class:`Dependency`.
        """
        cue_variable_name = cue_set_name_overrides.get(variable_name, variable_name)
        logger.info("Updating cue settings for dependency %s: %s", variable_name, version)
        self.update_settings(version.to_cue(cue_variable_name), overwrite=True)
        self.variable_to_version[variable_name] = version

        self._cue.add_dependency(cue_variable_name)

        if reset_configure:
            self.module_release_local.unlink(missing_ok=True)
            self.git_reset_repo_directory(variable_name, "configure")

        if add_to_group:
            self._check_group_is_ready()
            cache_path = self.get_path_for_version_info(version)
            makefile = self.get_makefile_for_path(cache_path)
            dep = Dependency.from_makefile(
                makefile,
                recurse=True,
                name=version.name,
                variable_name=variable_name,
                root=self.group,
            )
            self.variable_to_dependency[variable_name] = dep

            return dep

        return None

    @property
    def module_release_local(self) -> pathlib.Path:
        """
        The RELEASE.local file containing all dependencies, generated by cue.
        """
        return self.module_cache_path / "RELEASE.local"

    def _check_group_is_ready(self):
        if self.group is None:
            raise RuntimeError("epics-base version not yet set")
        if not self.introspection_paths.epics_base.exists():
            raise RuntimeError("epics-base for introspection / building not present")

    @folded_output("Finding all dependencies...")
    def find_all_dependencies(self):
        """
        Using module path conventions, find all dependencies and check them
        out to the cache directory.

        See Also
        --------
        :func:`VersionInfo.from_path`
        """
        checked = set()

        self._check_group_is_ready()

        def done() -> bool:
            assert self.group is not None
            return all(
                dep.variable_name in checked
                for dep in self.group.all_modules.values()
            )

        while not done():
            assert self.group is not None
            deps = list(self.group.all_modules.values())
            for dep in deps:
                if dep.variable_name in checked:
                    continue
                checked.add(dep.variable_name)

                logger.debug(
                    (
                        "Checking module %s for all dependencies. \n"
                        "\nExisting dependencies:\n"
                        "    %s"
                        "\nMissing paths: \n"
                        "    %s"
                    ),
                    dep.variable_name or "this IOC",
                    "\n    ".join(f"{var}={value}" for var, value in dep.dependencies.items()),
                    "\n    ".join(f"{var}={value}" for var, value in dep.missing_paths.items()),
                )

                for var, path in dep.missing_paths.items():
                    if var in checked:
                        version = self.variable_to_version.get(var, None)
                        if version is not None:
                            dep.dependencies[var] = self.get_path_for_version_info(version)
                        else:
                            logger.warning("Dependency still missing; %s", var)
                        continue

                    version_info = VersionInfo.from_path(path)
                    if version_info is None:
                        logger.debug(
                            "Dependency path for %s=%s does not match known patterns", var, path
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

    def use_epics_base(self, tag: str, build: bool = True, reset_configure: bool = True):
        """
        Add EPICS_BASE as a dependency given the provided tag.

        Parameters
        ----------
        tag : str
            The epics-base tag to use.
        build : bool, optional
            Build epics-base now.  Defaults to False.
        reset_configure : bool, optional
            Reset epics-base/configure/* to the git HEAD, if changed.
        """
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

        # with open(self.cache_path / "RELEASE_SITE", "wt") as fp:
        #     print(f"EPICS_SITE_TOP={cache_base}", file=fp)
        #     print(f"BASE_MODULE_VERSION={tag}", file=fp)
        #     print("EPICS_MODULES=$(EPICS_SITE_TOP)/modules", file=fp)

        # tagged_base_path = self.cache_path / "base" / tag
        # if tagged_base_path.exists() and tagged_base_path.is_symlink():
        #     tagged_base_path.unlink()

        # os.symlink(
        #     # modules/epics-base-... ->
        #     self.get_path_for_version_info(base_version),
        #     # base/tag/...
        #     tagged_base_path
        # )
        self.add_dependency(
            "EPICS_BASE",
            base_version,
            reset_configure=reset_configure,
            add_to_group=False,
        )

        base_path = self.get_path_for_version_info(base_version)

        cache_path = self.get_path_for_version_info(base_version)
        # TODO
        self._cue.places["EPICS_BASE"] = str(cache_path)

        if build:
            self.module_release_local.touch(exist_ok=True)
            self._cue.setup_for_build(CueOptions())
            self._cue.call_make(cwd=str(cache_path), parallel=4, silent=True)

        self.introspection_paths = PcdsBuildPaths(
            epics_base=base_path,
            epics_site_top=pathlib.Path("/cds/group/pcds/"),
            epics_modules=pathlib.Path(f"/cds/group/pcds/epics/{tag}/modules"),
        )

        self.group = self._create_dependency_group()
        dep = self.group.all_modules[self.group.root]
        logger.debug(
            (
                "Checking the primary target for dependencies after epics-base installation. \n"
                "\nExisting dependencies:\n"
                "    %s"
                "\nMissing paths: \n"
                "    %s"
            ),
            "\n    ".join(f"{var}={value}" for var, value in dep.dependencies.items()),
            "\n    ".join(f"{var}={value}" for var, value in dep.missing_paths.items()),
        )

    def _update_makefiles_in_path(self, base_path: pathlib.Path, makefile: Makefile):
        """
        Update makefiles found during the introspection step that exist in ``base_path``.

        Updates module dependency paths based.

        Parameters
        ----------
        base_path : pathlib.Path
            The path to update makefiles under.
        makefile : Makefile
            The primary Makefile that contains paths of relevant included makefiles.
        """
        for makefile_relative in makefile.makefile_list:
            makefile_path = (base_path / makefile_relative).resolve()
            try:
                makefile_path.relative_to(base_path)
            except ValueError:
                # logger.debug(
                #     "Skipping makefile: %s (not relative to %s)",
                #     makefile_path,
                #     base_path,
                # )
                continue

            try:
                patch_makefile(makefile_path, self.makefile_variables_to_patch)
            except PermissionError:
                logger.error("Failed to patch makefile due to permissions: %s", makefile_path)
            except Exception:
                logger.exception("Failed to patch makefile: %s", makefile_path)

    @folded_output("Updating makefiles")
    def update_makefiles(self):
        """
        Updates all makefiles with appropriate paths.

        * RELEASE.local from epics-ci cue
        * Dependency makefiles found during the introspection stage
        """
        for dep in self.variable_to_dependency.values():
            assert dep.variable_name is not None
            version = self.variable_to_version[dep.variable_name]
            dep_path = self.get_path_for_version_info(version)
            logger.debug("Updating RELEASE.local: %s=%s", dep.variable_name, dep_path)
            self._cue.update_release_local(dep.variable_name, str(dep_path))

            if dep in ("EPICS_BASE", "BASE"):  # argh, why can't I remember which
                continue

            self._update_makefiles_in_path(dep_path, dep.makefile)

        self._update_makefiles_in_path(self.target_path, self.ioc_makefile)

    @property
    def ioc(self) -> Dependency:
        self._check_group_is_ready()
        assert self.group is not None
        return self.group.all_modules[self.group.root]

    @property
    def ioc_makefile(self) -> Makefile:
        return self.ioc.makefile

    @property
    def makefile_variables_to_patch(self) -> Dict[str, str]:
        """Variables to patch in module makefiles."""
        to_patch = {
            variable: str(path)
            for variable, path in self.dependency_to_path.items()
        }
        # Use EPEL re2c for now instead of the pspkg one:
        to_patch["RE2C"] = "re2c"
        return to_patch

    @property
    def dependency_to_path(self) -> Dict[str, pathlib.Path]:
        """Dependency variable name to cache path."""
        return {
            var: self.get_path_for_version_info(version)
            for var, version in self.variable_to_version.items()
        }

    def update_build_order(self) -> List[str]:
        """Update cue's build order of the modules to compile."""
        build_order = self.get_build_order()
        self._cue.modules_to_compile[:] = [
            cue_set_name_overrides.get(variable, variable)
            for variable in build_order
        ]
        return build_order


def main(command: str, ioc_path: str):
    assert command in ("prepare", "build")
    preparing = command == "prepare"

    with cue.Folded("cue.shim", "Preparing the cue shim..."):
        cue_shim = CueShim(
            target_path=pathlib.Path(ioc_path).resolve(),
            local="GITHUB_ACTIONS" not in os.environ,
        )

    # NOTE/TODO: use the 7.0.3.1-2.0 *branch* for noW:
    # R7.0.3.1-2.0 is a branch, whereas R7.0.3.1-2.0.1 is a tag;

    with cue.Folded("use.epics.base", "Preparing EPICS base..."):
        cue_shim.use_epics_base("R7.0.2-2.branch", build=preparing, reset_configure=preparing)

    # /cds/group/pcds/epics/base/R7.0.3.1-2.0 is where all minor local fixes
    # go for 7.0.3.1-2.0.
    # cue_shim.use_epics_base("R7.0.3.1-2.0-branch")
    if preparing:
        cue_shim.find_all_dependencies()
        with cue.Folded("write.set", "Writing the default set..."):
            cue_shim.write_set_to_file("defaults")

        cue_shim.update_makefiles()
        with cue.Folded("update.makefiles", "Updating the build order..."):
            cue_shim.update_build_order()

        with cue.Folded("cue.prepare", "Calling cue.prepare..."):
            # TODO: slac-epics/epics-base has absolute /afs submodule paths :(
            cue_shim._cue.prepare(CueOptions())
    else:
        with cue.Folded("cue.build", "Calling cue.build..."):
            cue_shim._cue.build(CueOptions(makeargs=["-C", str(cue_shim.target_path)]))


if __name__ == "__main__":
    logging.basicConfig(level="WARNING")
    logger.setLevel("DEBUG")
    main(command=sys.argv[1], ioc_path=sys.argv[2])
