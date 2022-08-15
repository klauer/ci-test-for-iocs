from __future__ import annotations

from typing import ClassVar, List, Optional
import os
import pathlib
import re
from dataclasses import dataclass

from whatrecord.makefile import Makefile
from whatrecord.makefile import DependencyGroup


@dataclass
class EpicsDependency:
    base: str
    name: str
    tag: str

    _module_path_regexes_: ClassVar[List[re.Pattern]] = [
        re.compile(
            base_path + "/"
            r"{base_path}/"
            r"(?P<base>[^/]+)/"
            r"modules/"
            r"(?P<name>[^/]+)/"
            r"(?P<tag>[^/]+)/?"
        )
        for base_path in ("/cds/group/pcds/epics", "/reg/g/pcds/epics")
    ]

    @classmethod
    def from_path(cls, path: pathlib.Path) -> Optional[EpicsDependency]:
        path_str = str(path.resolve())
        # TODO some sort of configuration
        for regex in cls._module_path_regexes_:
            match = regex.match(path_str)
            if match is None:
                continue
            return cls(**match.groupdict())
        return None


makefile = Makefile.from_file(
    Makefile.find_makefile("ads-ioc"),
    keep_os_env=False,
    variables=dict(EPICS_BASE="/Users/klauer/Repos/epics-base"),
)

info = DependencyGroup.from_makefile(makefile, recurse=True, keep_os_env=False)

this_ioc = info.all_modules[info.root]

for var, path in this_ioc.missing_paths.items():
    print(path, "->", EpicsDependency.from_path(path))

MODULE_PATH = pathlib.Path(__file__).parent.resolve()
os.environ["CACHEDIR"] = str(MODULE_PATH / "cache")


from cue import prepare_env, detect_context, ci as cue_ci_context

prepare_env()
detect_context()
print(cue_ci_context)
