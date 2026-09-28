"""Record source/download correspondence without changing a release or accessing the network.

Usage: python tools/release/source_manifest.py --source-ref <commit> --output release.json
       python tools/release/source_manifest.py --source-ref <commit> --verify-file Setup.exe
Run from the public source checkout, or pass --source-root. This records the
catalogue committed with that source; it is not a reproducible-build attestation.
When the catalogue has a Linux beta entry (catalogue.linux), its player ZIP and its
corresponding-source ZIP are recorded too, and --verify-file accepts either ZIP.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

CLAIM = "Download hashes recorded in this exact source commit; not a bit-for-bit build attestation."
LINUX_CLAIM = ("The Linux beta's corresponding source is the linux-source-zip artifact, not this repository: "
               "its SOURCE-MANIFEST.json names the private source commit it was exported from, with "
               "private-only files left out.")


def _artifact(kind, version, location, sha256, size):
    url = urlsplit(location) if isinstance(location, str) else None
    if (url is None or url.scheme != "https" or not url.netloc or url.username or url.password
            or not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256)
            or type(size) is not int or size <= 0):
        raise ValueError("Invalid release catalogue artifact")
    return {"kind": kind, "version": version,
            "filename": url.path.rsplit("/", 1)[-1], "url": location,
            "sha256": sha256, "size": size}


def make_manifest(root, ref):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], encoding="utf-8").strip()
    commit = git("rev-parse", "--verify", "--end-of-options", str(ref) + "^{commit}")
    if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
        raise ValueError("An immutable source commit is required")
    catalogue = json.loads(git("show", commit + ":server/public/catalogue.json"))
    artifacts = []
    for kind, entry, field in [("windows-installer", catalogue["hub"], "download_url"),
                                *((m["id"], m, "pack_url") for m in catalogue.get("gamemodes", []))]:
        artifacts.append(_artifact(kind, entry["version"], entry[field], entry["sha256"], entry["size"]))
    manifest = {"schema_version": 1, "release": catalogue["hub"]["version"]}
    linux = catalogue.get("linux")
    if linux is not None:
        # The top-level entry beside hub that publish.py linux writes. It is recorded after the
        # Windows and pack artifacts, which stay exactly as they were.
        if (not isinstance(linux, dict) or linux.get("kind") != "linux-native-zip"
                or linux.get("arch") != "x86_64" or not isinstance(linux.get("version"), str)):
            raise ValueError("Invalid Linux catalogue entry")
        artifacts.append(_artifact("linux-native-zip", linux["version"], linux.get("download_url"),
                                   linux.get("sha256"), linux.get("size")))
        artifacts.append(_artifact("linux-source-zip", linux["version"], linux.get("source_url"),
                                   linux.get("source_sha256"), linux.get("source_size")))
        manifest["linux_release"] = linux["version"]
    manifest.update({"source_commit": commit, "catalogue_path": "server/public/catalogue.json",
                     "claim": CLAIM if linux is None else CLAIM + " " + LINUX_CLAIM,
                     "artifacts": artifacts})
    return manifest


def verify_artifact(path, artifact):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if path.stat().st_size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        raise ValueError("Download does not match the committed source catalogue")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-file", type=Path)
    args = parser.parse_args(argv)
    manifest = make_manifest(args.source_root, args.source_ref)
    if args.verify_file:
        matches = [a for a in manifest["artifacts"] if a["filename"] == args.verify_file.name]
        if len(matches) != 1:
            parser.error("File name must identify one artifact in the selected source catalogue")
        verify_artifact(args.verify_file, matches[0])
        print("Download matches source commit " + manifest["source_commit"])
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    elif not args.verify_file:
        print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
