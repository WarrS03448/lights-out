"""Record source/download correspondence without changing a release or accessing the network.

Usage: python tools/release/source_manifest.py --source-ref <commit> --output release.json
       python tools/release/source_manifest.py --source-ref <commit> --verify-file Setup.exe
Run from the public source checkout, or pass --source-root. This records the
catalogue committed with that source; it is not a reproducible-build attestation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


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
        url = urlsplit(entry[field])
        if (url.scheme != "https" or not url.netloc or url.username or url.password
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                or type(entry["size"]) is not int or entry["size"] <= 0):
            raise ValueError("Invalid release catalogue artifact")
        artifacts.append({"kind": kind, "version": entry["version"],
                          "filename": url.path.rsplit("/", 1)[-1], "url": entry[field],
                          "sha256": entry["sha256"], "size": entry["size"]})
    return {"schema_version": 1, "release": catalogue["hub"]["version"],
            "source_commit": commit, "catalogue_path": "server/public/catalogue.json",
            "claim": "Download hashes recorded in this exact source commit; not a bit-for-bit build attestation.",
            "artifacts": artifacts}


def verify_artifact(path, artifact):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if path.stat().st_size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        raise ValueError("Download does not match the committed source catalogue")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-file", type=Path)
    args = parser.parse_args()
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
