"""Shared build helpers; all generated files stay inside this checkout."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "working"
LOGS = ROOT / "logs"
VERSIONS = json.loads((ROOT / "versions.json").read_text())


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def fingerprint(*paths, extra=""):
    result = hashlib.sha256(extra.encode())
    for path in sorted(map(Path, paths)):
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for file in files:
            result.update(str(file.relative_to(ROOT)).encode())
            result.update(bytes.fromhex(digest(file)))
    return result.hexdigest()


def command(args, *, cwd=None, env=None, log=None, input=None, stream=False):
    args = list(map(str, args))
    if log:
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / log).open("ab") as output:
            output.write(("\n$ " + " ".join(args) + "\n").encode())
            output.flush()
            if stream:
                with subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT) as process:
                    while True:
                        chunk = process.stdout.read1(8192)
                        if not chunk:
                            break
                        output.write(chunk)
                        output.flush()
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    result = subprocess.CompletedProcess(args, process.wait())
            else:
                result = subprocess.run(args, cwd=cwd, env=env, input=input,
                                        stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            tail = (LOGS / log).read_text(errors="replace").splitlines()[-16:]
            raise RuntimeError(f"Command failed; see {LOGS / log}\n" + "\n".join(tail))
        return result
    return subprocess.run(args, cwd=cwd, env=env, input=input, check=True)


def find_tool(name):
    for candidate in (shutil.which(name), f"/usr/sbin/{name}", f"/sbin/{name}"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(f"Missing host tool: {name}")


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def cached(stamp, key, outputs):
    return stamp.is_file() and stamp.read_text().strip() == key and all(p.exists() for p in outputs)


def download(name, directory):
    info = VERSIONS["downloads"][name]
    destination = directory / info["file"]
    receipt = destination.with_name(destination.name + ".verified.json")
    if destination.exists() and receipt.exists():
        stat = destination.stat()
        expected = {"sha256": info["sha256"], "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        if json.loads(receipt.read_text()) == expected:
            return destination
    if not destination.exists():
        print(f"Downloading {info['file']}...", flush=True)
        temporary = destination.with_name(destination.name + ".partial")
        command(["curl", "-fL", "--retry", "3", "--connect-timeout", "30",
                 "-o", temporary, info["url"]])
        if digest(temporary) != info["sha256"]:
            temporary.unlink()
            raise RuntimeError(f"Checksum mismatch for {info['file']}")
        temporary.replace(destination)
    if digest(destination) != info["sha256"]:
        raise RuntimeError(f"Checksum mismatch: remove {destination} and retry")
    stat = destination.stat()
    write_json(receipt, {"sha256": info["sha256"], "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return destination


def checkout(name, destination):
    info = VERSIONS["sources"][name]
    stamp = destination / ".g925-upstream"
    if cached(stamp, info["commit"], [destination / ".git"]):
        return
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    print(f"Fetching pinned {name} source...", flush=True)
    log = f"fetch-{name}.log"
    command(["git", "init", destination], log=log)
    command(["git", "-C", destination, "remote", "add", "origin", info["url"]], log=log)
    urls = [info["url"], *info.get("fallback_urls", [])]
    for index, url in enumerate(urls):
        command(["git", "-C", destination, "remote", "set-url", "origin", url], log=log)
        try:
            command(["git", "-C", destination, "-c", "http.version=HTTP/1.1",
                     "fetch", "--progress", "--depth=1", "origin", info["commit"]],
                    log=log, stream=True)
            break
        except RuntimeError:
            if index + 1 == len(urls):
                raise
            print(f"Fetch failed; trying alternate source for {name}...", flush=True)
    command(["git", "-C", destination, "checkout", "--detach", "FETCH_HEAD"], log=log)
    actual = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"]).decode().strip()
    if actual != info["commit"]:
        raise RuntimeError(f"Wrong {name} commit: {actual}")
    stamp.write_text(info["commit"] + "\n")


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise RuntimeError(f"Unsafe archive member: {name}")
    return path


def unpack_tar(archive, destination, *, strip=0):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for member in source:
            parts = safe_name(member.name).parts
            if len(parts) <= strip:
                continue
            member.name = str(PurePosixPath(*parts[strip:]))
            if member.issym() or member.islnk():
                target = member.linkname
                if target.startswith("/"):
                    # Vendored EDK2 includes an optional macOS X11 header
                    # link. External host paths are never extracted.
                    continue
                base = (destination / member.name).parent if member.issym() else destination
                if not (base / target).resolve().is_relative_to(destination.resolve()):
                    raise RuntimeError(f"Archive link escapes extraction directory: {target}")
                if member.islnk() and strip:
                    member.linkname = str(PurePosixPath(*safe_name(target).parts[strip:]))
            if member.isdev() or member.isfifo():
                raise RuntimeError(f"Unsupported archive member: {member.name}")
            source.extract(member, destination)


def unpack_zip(archive, destination, prefix=""):
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            safe_name(member.filename)
            if not member.filename.startswith(prefix) or member.is_dir():
                continue
            target = destination / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = member.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                link = source.read(member).decode()
                if not (target.parent / link).resolve().is_relative_to(destination.resolve()):
                    raise RuntimeError(f"Archive link escapes extraction directory: {link}")
                target.symlink_to(link)
                continue
            with source.open(member) as input_file, target.open("wb") as output:
                shutil.copyfileobj(input_file, output, 4 * 1024 * 1024)
            if mode:
                target.chmod(mode & 0o777)
