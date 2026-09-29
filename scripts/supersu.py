"""Use the SuperSU bundle shipped with the pinned Hacker Kernel source."""

from common import WORK, VERSIONS, digest


def bundle():
    configuration = VERSIONS["supersu"]
    directory = WORK / "kernel-source" / configuration["source_path"]
    for name, expected in configuration["sha256"].items():
        source = directory / name
        if not source.is_file() or digest(source) != expected:
            raise RuntimeError(f"Missing or modified SuperSU {configuration['version']} file: {source}")
    return directory
