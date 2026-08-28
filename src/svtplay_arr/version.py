"""The running package's own version, read back from its installed metadata.

pyproject.toml carries no static `version` field any more. Three releases
(v0.1.0, v0.2.0, v0.3.0) shipped with one that nobody remembered to bump, so
`/health` and the installer's own upgrade footer kept reporting `0.1.0` for
two releases while a different version actually ran. hatch-vcs now derives
the version from the git tag at build time -- see the comment beside
`[tool.hatch.version]` in pyproject.toml -- so the number baked into this
package's own dist-info is the one honest source for it, and it cannot go
stale the same way twice.

This module does not re-derive anything from git: the resolved version
already lives in the installed package's metadata, and reading it a second
time from git here would just be a second place for this exact defect to
recur in. `importlib.metadata` reads that metadata back.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

# The distribution name in pyproject.toml, not the import package name
# (`svtplay_arr`) -- importlib.metadata looks distributions up by the
# former.
_DISTRIBUTION_NAME = "svtplay-arr"

UNKNOWN = "unknown"


def service_version() -> str:
    """This package's installed version, or "unknown" if it cannot be read.

    Never raises. `/health` and the config page must both survive this the
    same way they survive every other fact they report: a monitoring
    endpoint that 500s because its own version lookup failed would be a
    worse outcome than an honest "unknown". `PackageNotFoundError` is the
    one realistic way to land here -- an install whose dist-info is
    missing or unreadable -- and "unknown" is the honest answer for that
    case, not a guess dressed up as a number.
    """
    try:
        return _installed_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return UNKNOWN
