"""Extract only required Odin partitions from a user supplied firmware RAR."""

import hashlib
import json
import shutil
import struct
import subprocess
import tarfile
from pathlib import Path

from common import ROOT, WORK, command, digest, find_tool, safe_name, write_json

SUPPORTED_SURFACEFLINGER = "ed450318ad6667f3ae3892a576521bd7f217b209ce18f44e2b0259307e156b3e"
SUPPORTED_SERVICES = "1e6f84db3b606ef0d28c981c561d065fb8dcf9548bad6bbeafce7a32d02bd85a"


def quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def debugfs(image, instruction, *, write=False):
    args = [find_tool("debugfs")] + (["-w"] if write else [])
    result = subprocess.run(args + ["-R", instruction, str(image)],
                            capture_output=True, text=True, check=True)
    # debugfs often exits zero even when the filesystem command fails.
    failures = ("File not found", "Command not found", "Usage:", "Could not allocate",
                "No free space", "while opening", "while writing", "while setting",
                "Filesystem is read-only", "Bad magic")
    if any(token in result.stderr for token in failures):
        raise RuntimeError(f"debugfs {instruction}: {result.stderr.strip()}")
    return result.stdout


def dump(image, source, target):
    debugfs(image, f"dump {quote(source)} {quote(target)}")
    if not target.is_file():
        raise RuntimeError(f"Failed to read {source} from {image}")


def expand_sparse(source, target):
    temporary = target.with_name(target.name + ".partial")
    with source.open("rb") as input_file, temporary.open("wb") as output:
        header = input_file.read(28)
        if header[:4] != bytes.fromhex("3aff26ed"):
            input_file.seek(0)
            shutil.copyfileobj(input_file, output, 4 * 1024 * 1024)
        else:
            magic, major, minor, file_header, chunk_header, block_size, blocks, chunks, checksum = struct.unpack("<IHHHHIIII", header)
            if major != 1 or file_header < 28 or chunk_header < 12 or block_size % 4 or not block_size:
                raise RuntimeError("Unsupported Android sparse image header")
            input_file.seek(file_header)
            written_blocks = 0
            for index in range(chunks):
                chunk = input_file.read(12)
                if len(chunk) != 12:
                    raise RuntimeError("Truncated sparse chunk header")
                kind, reserved, count, size = struct.unpack("<HHII", chunk)
                input_file.seek(chunk_header - 12, 1)
                length = count * block_size
                payload_size = {0xCAC1: length, 0xCAC2: 4, 0xCAC3: 0, 0xCAC4: 4}.get(kind)
                if payload_size is None or size != chunk_header + payload_size:
                    raise RuntimeError(f"Invalid sparse chunk {index}")
                if kind == 0xCAC1:
                    left = length
                    while left:
                        data = input_file.read(min(left, 4 * 1024 * 1024))
                        if not data:
                            raise RuntimeError("Truncated sparse image payload")
                        output.write(data)
                        left -= len(data)
                elif kind == 0xCAC2:
                    fill = input_file.read(4)
                    if len(fill) != 4:
                        raise RuntimeError("Truncated fill chunk")
                    if fill == b"\0" * 4:
                        output.seek(length, 1)
                    else:
                        buffer = fill * (min(length, 1024 * 1024) // 4)
                        while length:
                            data = buffer[:length]
                            output.write(data)
                            length -= len(data)
                elif kind == 0xCAC3:
                    output.seek(length, 1)
                elif len(input_file.read(4)) != 4 or count != 0:
                    raise RuntimeError("Invalid checksum chunk")
                if kind != 0xCAC4:
                    written_blocks += count
            if written_blocks != blocks:
                raise RuntimeError("Sparse image block count mismatch")
            output.truncate(blocks * block_size)
    temporary.replace(target)


def extract_odin(rar, member, wanted, destination):
    print(f"Extracting {Path(member).name}...", flush=True)
    process = subprocess.Popen(["bsdtar", "-xOf", str(rar), member],
                               stdout=subprocess.PIPE)
    found = set()
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
            for entry in archive:
                safe_name(entry.name)
                name = Path(entry.name).name
                if name not in wanted:
                    continue
                if not entry.isfile() or name in found:
                    raise RuntimeError(f"Invalid or duplicate Odin partition: {name}")
                with archive.extractfile(entry) as source, (destination / name).open("wb") as output:
                    shutil.copyfileobj(source, output, 4 * 1024 * 1024)
                found.add(name)
        # Drain the Odin MD5 trailer and let libarchive finish its RAR CRC check.
        while process.stdout.read(1024 * 1024):
            pass
        if process.wait() or found != set(wanted):
            raise RuntimeError(f"Could not extract required partitions: {set(wanted) - found}")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            process.wait()


def find_cached_firmware(identifier=None):
    candidates = []
    expected = {"/lib64/libsurfaceflinger.so": SUPPORTED_SURFACEFLINGER,
                "/framework/arm64/services.odex": SUPPORTED_SERVICES}
    for receipt in sorted((WORK / "firmware").glob("*/firmware.json")):
        directory = receipt.parent
        metadata = json.loads(receipt.read_text())
        key = metadata.get("rar_sha256", "")
        if (not isinstance(key, str) or len(key) != 64 or
                any(character not in "0123456789abcdef" for character in key) or
                directory.name != key[:16] or metadata.get("binary_sha256") != expected):
            continue
        required = (directory / "stock.raw.img", directory / "hidden.raw.img", directory / "boot/ramdisk")
        if all(path.is_file() for path in required):
            candidates.append((directory, key))
    if identifier:
        candidates = [(directory, key) for directory, key in candidates
                      if key.startswith(identifier)]
    if not candidates:
        raise RuntimeError("No matching extracted firmware cache found. Run ./run.sh /path/to/firmware.rar once.")
    if len(candidates) != 1:
        choices = ", ".join(directory.name for directory, key in candidates)
        raise RuntimeError(f"Several firmware caches exist ({choices}). Select one with --firmware-id ID.")
    directory, key = candidates[0]
    print(f"Using extracted firmware cache: {directory.name}", flush=True)
    return directory, key


def prepare_firmware(rar):
    key = digest(rar)
    directory = WORK / "firmware" / key[:16]
    receipt = directory / "firmware.json"
    required = [directory / "stock.raw.img", directory / "hidden.raw.img", directory / "boot/ramdisk"]
    if receipt.exists() and all(path.exists() for path in required):
        if json.loads(receipt.read_text())["rar_sha256"] == key:
            return directory, key
    directory.mkdir(parents=True, exist_ok=True)
    listing = subprocess.check_output(["bsdtar", "-tf", str(rar)], text=True)
    paths = listing.splitlines()
    ap = [path for path in paths if Path(path).name.startswith("AP_") and path.endswith((".tar.md5", ".tar"))]
    csc = [path for path in paths if Path(path).name.startswith("CSC_") and path.endswith((".tar.md5", ".tar"))]
    if len(ap) != 1 or len(csc) != 1 or "G925FXXU1AOCV" not in Path(ap[0]).name:
        raise RuntimeError("Provide the SM-G925F G925FXXU1AOCV Android 5.0.2 repair firmware RAR (AP and CSC).")
    extract_odin(rar, ap[0], {"boot.img", "system.img"}, directory)
    extract_odin(rar, csc[0], {"hidden.img"}, directory)
    expand_sparse(directory / "system.img", directory / "stock.raw.img")
    expand_sparse(directory / "hidden.img", directory / "hidden.raw.img")
    if (directory / "boot").exists():
        shutil.rmtree(directory / "boot")
    command(["python3", ROOT / "scripts/unpack-boot.py", directory / "boot.img",
             "--output-dir", directory / "boot"], log="extract-boot.log")
    hashes = {}
    for source, expected in (("/lib64/libsurfaceflinger.so", SUPPORTED_SURFACEFLINGER),
                             ("/framework/arm64/services.odex", SUPPORTED_SERVICES)):
        target = directory / Path(source).name
        dump(directory / "stock.raw.img", source, target)
        actual = digest(target)
        target.unlink()
        if actual != expected:
            raise RuntimeError(f"Unsupported firmware binary {source}; patches cannot be applied safely.")
        hashes[source] = actual
    write_json(receipt, {"rar_sha256": key, "ap": ap[0], "csc": csc[0], "binary_sha256": hashes})
    for name in ("system.img", "hidden.img"):
        (directory / name).unlink()
    print("Firmware extracted and matched to the supported patches.", flush=True)
    return directory, key
