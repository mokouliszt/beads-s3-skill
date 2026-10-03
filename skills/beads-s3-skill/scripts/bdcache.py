#!/usr/bin/env python3
"""Install the bd release recorded in the bucket, using the bucket as a
download cache.

  bdcache.py ensure <default-version>   install the bucket's bd version
                                        (records <default-version> first if
                                        the bucket has none); prints bd's path
  bdcache.py install <version>          install that version, bucket unchanged
  bdcache.py latest                     print the newest bd release
  bdcache.py set-version <version>      record a new bd version for the bucket
  bdcache.py projects                   list project names in the bucket

Bucket layout (under the configured prefix, default "beads/"):
  bd/VERSION                     bd release used with this bucket
  bd/<version>/<os>-<arch>.tar.gz  cached release archive
  <project>.bundle               project databases
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader(
    "s3bundle", os.path.join(HERE, "git-remote-s3bundle"))
_spec = importlib.util.spec_from_loader("s3bundle", _loader)
s3bundle = importlib.util.module_from_spec(_spec)
_loader.exec_module(s3bundle)

RELEASES = "https://github.com/gastownhall/beads/releases"
NPM_PACKAGE = "@beads/bd"


def log(msg):
    sys.stderr.write("[beads-s3] %s\n" % msg)
    sys.stderr.flush()


def settings():
    cfg = s3bundle.load_config()
    bucket = cfg.get("bucket")
    if not bucket:
        sys.exit("[beads-s3] error: \"bucket\" is missing in the settings")
    prefix = cfg.get("prefix", "beads/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return cfg, bucket, prefix


def is_missing(exc):
    code, status = s3bundle.error_info(exc)
    return status == 404 or code in ("NoSuchKey", "404", "NotFound")


def read_text(s3, bucket, key):
    from botocore.exceptions import ClientError
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode().strip()
    except ClientError as exc:
        if is_missing(exc):
            return None
        raise


def target():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if not arch:
        sys.exit("[beads-s3] error: unsupported CPU: %s" % machine)
    return system, arch


def installed_version(path):
    try:
        out = subprocess.run([path, "version"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"version (\S+)", out)
    return m.group(1) if m else None


def bin_dir():
    current = shutil.which("bd")
    if current and os.access(os.path.dirname(current), os.W_OK):
        return os.path.dirname(current)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and d != "." and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    d = os.path.expanduser("~/.local/bin")
    os.makedirs(d, exist_ok=True)
    return d


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dest):
    with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def from_github(version, system, arch, work):
    name = "beads_%s_%s_%s.tar.gz" % (version, system, arch)
    base = "%s/download/v%s" % (RELEASES, version)
    archive = os.path.join(work, name)
    fetch("%s/%s" % (base, name), archive)
    sums = os.path.join(work, "checksums.txt")
    fetch("%s/checksums.txt" % base, sums)
    expected = None
    with open(sums, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) == 2 and parts[1] == name:
                expected = parts[0]
    if expected is None or sha256_of(archive) != expected:
        raise RuntimeError("checksum mismatch for %s" % name)
    return archive


def from_npm(version, work):
    out = subprocess.run(["npm", "pack", "--silent", "%s@%s" % (NPM_PACKAGE, version)],
                         cwd=work, capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "npm pack failed")
    return os.path.join(work, out.stdout.strip().splitlines()[-1])


def extract_bd(archive, work):
    with tarfile.open(archive) as tar:
        member = next((m for m in tar.getmembers()
                       if m.isfile() and os.path.basename(m.name) == "bd"), None)
        if member is None:
            raise RuntimeError("no bd binary in %s" % os.path.basename(archive))
        src = tar.extractfile(member)
        path = os.path.join(work, "bd")
        with open(path, "wb") as f:
            shutil.copyfileobj(src, f)
    os.chmod(path, 0o755)
    return path


def ensure(default_version):
    cfg, bucket, prefix = settings()
    s3 = s3bundle.make_client(cfg)
    marker = prefix + "bd/VERSION"
    version = read_text(s3, bucket, marker)
    if not version:
        version = default_version
        s3.put_object(Bucket=bucket, Key=marker, Body=(version + "\n").encode())
        log("recorded bd %s for this bucket" % version)
    install(version, cfg, bucket, prefix, s3)


def install(version, cfg=None, bucket=None, prefix=None, s3=None):
    from botocore.exceptions import ClientError
    if cfg is None:
        cfg, bucket, prefix = settings()
        s3 = s3bundle.make_client(cfg)
    current = shutil.which("bd")
    if current and installed_version(current) == version:
        print(current)
        return

    system, arch = target()
    key = "%sbd/%s/%s-%s.tar.gz" % (prefix, version, system, arch)
    work = tempfile.mkdtemp()
    try:
        archive = os.path.join(work, "bd.tar.gz")
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            s3.download_file(bucket, key, archive)
            if sha256_of(archive) != head.get("Metadata", {}).get("sha256"):
                raise RuntimeError("cached archive is corrupt")
            log("using cached bd %s from the bucket" % version)
        except (ClientError, RuntimeError) as exc:
            if isinstance(exc, ClientError) and not is_missing(exc):
                raise
            try:
                log("downloading bd %s from GitHub releases" % version)
                archive = from_github(version, system, arch, work)
            except Exception as gh_exc:  # noqa: BLE001 - fall back to npm
                log("GitHub download failed (%s); trying npm" % gh_exc)
                archive = from_npm(version, work)
            s3.upload_file(archive, bucket, key,
                           ExtraArgs={"Metadata": {"sha256": sha256_of(archive)}})
            log("cached bd %s in the bucket" % version)
        binary = extract_bd(archive, work)
        if installed_version(binary) != version:
            raise RuntimeError("downloaded bd does not report version %s" % version)
        dest = os.path.join(bin_dir(), "bd")
        if os.path.lexists(dest):
            os.unlink(dest)
        shutil.copy2(binary, dest)
        print(dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def latest():
    req = urllib.request.Request(RELEASES + "/latest", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            m = re.search(r"/tag/v([^/?#]+)", resp.geturl())
            if m:
                return m.group(1)
    except OSError:
        pass
    with urllib.request.urlopen("https://registry.npmjs.org/%s/latest" % NPM_PACKAGE.replace("/", "%2F"),
                                timeout=60) as resp:
        return json.load(resp)["version"]


def set_version(version):
    cfg, bucket, prefix = settings()
    s3bundle.make_client(cfg).put_object(Bucket=bucket, Key=prefix + "bd/VERSION",
                                         Body=(version + "\n").encode())


def projects():
    cfg, bucket, prefix = settings()
    s3 = s3bundle.make_client(cfg)
    names = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix,
                                                             Delimiter="/"):
        for obj in page.get("Contents", []):
            rest = obj["Key"][len(prefix):]
            if rest.endswith(".bundle"):
                names.append(rest[:-len(".bundle")])
    return names


def main(argv):
    if len(argv) == 3 and argv[1] == "ensure":
        ensure(argv[2])
    elif len(argv) == 3 and argv[1] == "install":
        install(argv[2])
    elif len(argv) == 2 and argv[1] == "latest":
        print(latest())
    elif len(argv) == 3 and argv[1] == "set-version":
        set_version(argv[2])
    elif len(argv) == 2 and argv[1] == "projects":
        print("\n".join(projects()))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
