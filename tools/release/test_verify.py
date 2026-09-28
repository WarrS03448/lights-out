"""Does publish.py verify() actually catch a wrong build behind a catalogue entry?

"It compiles" does not test that. Every HTTP call verify() makes goes to a fake site held in memory
(publish.http is replaced; nothing touches the network or production), and verify() is driven
against it:

  1. served bytes match the catalogue            -> passes
  2. same LENGTH, different bytes                -> must DIE (a HEAD-only check passed this)
  3. wrong length                                -> must die
  4. catalogue entry carries no sha256           -> passes, but says the bytes are unverified
  5. the Linux ZIP or its source ZIP is wrong    -> must die, by hash and by size
  6. no Linux entry                              -> no Linux request at all
  7. a Linux update record the client refuses    -> must die (it deploys and serves fine, and every
                                                    installed client silently never offers it)

Run:  python -m pytest -q tools/release/test_verify.py   (or python tools/release/test_verify.py)
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.error
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish  # noqa: E402
import linux_update_trust as trust  # noqa: E402

BASE = "https://site.test"
HUB = b"PRETEND-INSTALLER-" + b"A" * 400
IMPOSTOR = b"PRETEND-INSTALLER-" + b"B" * 400           # same length, different build
PACK = b"PRETEND-PACK"
SOURCE = b"PRETEND-SOURCE-ZIP-" + b"S" * 500
FOLDER = "LightsOut-Linux-Native-9.9.9"
KEY, OTHER = bytes([7]) * 32, bytes([8]) * 32          # test Ed25519 seeds, never production keys
assert len(HUB) == len(IMPOSTOR)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ed25519_sign(seed, message):
    """(public key, signature): RFC 8032 section 5.1.6, as sign_update.pure_sign does it."""
    digest = hashlib.sha512(seed).digest()
    scalar = (int.from_bytes(digest[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    public = trust._encode(trust._multiply(scalar, trust._BASE))
    nonce = trust._challenge(digest[32:], message)
    commitment = trust._encode(trust._multiply(nonce, trust._BASE))
    s = (nonce + trust._challenge(commitment, public, message) * scalar) % trust._L
    return public, commitment + s.to_bytes(32, "little")


def linux_zip(*seeds, trust_member=True):
    """A player ZIP whose runtime pins these keys (all verify() reads from it besides its bytes)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(FOLDER + "/READ ME.txt", b"Lights Out - native Linux beta\n")
        if trust_member:
            pins = "".join(f"    {trust.key_id(ed25519_sign(s, b'')[0])!r}: "
                           f"bytes.fromhex({ed25519_sign(s, b'')[0].hex()!r}),\n" for s in seeds)
            z.writestr(FOLDER + "/support/runtime/hub/update_trust_linux.py", f"PINNED_KEYS = {{\n{pins}}}\n")
    return buffer.getvalue()


LINUX = linux_zip(KEY)


def update_record(linux=LINUX, *, signer=KEY, key_of=None, archive_sha=None):
    """sign_update's record for the 9.9.9 ZIP `linux`, signed by `signer` under the key_id of `key_of`."""
    manifest = dict(trust.FIXED, version=[9, 9, 9, 0], display_version="9.9.9", release="9.9.9", folder=FOLDER,
                    archive={"name": FOLDER + "-x86_64.zip", "size": len(linux), "sha256": archive_sha or sha(linux)},
                    source={"name": FOLDER + "-source.zip", "size": len(SOURCE), "sha256": sha(SOURCE)},
                    bundle_sha256="1" * 64, payload_sha256="2" * 64, launcher_sha256="3" * 64,
                    installed_bytes=66794479)
    raw = trust.canonical(manifest)
    signature = ed25519_sign(signer, trust.DOMAIN + raw)[1]
    return {"key_id": trust.key_id(ed25519_sign(key_of or signer, b"")[0]),
            "manifest": base64.b64encode(raw).decode(), "signature": base64.b64encode(signature).decode()}


def catalogue(*, hub_sha=True, linux=True, linux_zip_bytes=LINUX, update=None):
    hub = {"version": "9.9.9", "download_url": BASE + "/hub/Setup-9.9.9.exe", "page_url": BASE + "/",
           "size": len(HUB), "kind": "inno-setup"}
    if hub_sha:
        hub["sha256"] = sha(HUB)
    c = {"hub": hub, "gamemodes": [{"id": "BB5", "version": "1.0.0", "pack_url": BASE + "/packs/BB5-1.0.0.zip",
                                    "size": len(PACK), "sha256": sha(PACK)}]}
    if linux:
        c["linux"] = {"version": "9.9.9", "kind": "linux-native-zip", "arch": "x86_64",
                      "download_url": BASE + "/hub/LightsOut-Linux-Native-9.9.9-x86_64.zip",
                      "size": len(linux_zip_bytes), "sha256": sha(linux_zip_bytes),
                      "source_url": BASE + "/hub/LightsOut-Linux-Native-9.9.9-source.zip",
                      "source_size": len(SOURCE), "source_sha256": sha(SOURCE),
                      "page_url": BASE + "/", "required": False,
                      "update": update or update_record(linux_zip_bytes)}
    return c


class FakeSite:
    """What the live site would answer: {url: body}; a missing url is a 404."""

    def __init__(self, local, linux=LINUX):
        self.files = {BASE + "/catalogue.json": json.dumps(local).encode(),
                      BASE + "/api/health": b'{"ok":true}',
                      BASE + "/hub/Setup-9.9.9.exe": HUB,
                      BASE + "/packs/BB5-1.0.0.zip": PACK,
                      BASE + "/hub/LightsOut-Linux-Native-9.9.9-x86_64.zip": linux,
                      BASE + "/hub/LightsOut-Linux-Native-9.9.9-source.zip": SOURCE}
        self.requests = []

    def http(self, url, method="GET", timeout=30):
        self.requests.append((method, url))
        if url not in self.files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        body = self.files[url]
        return 200, {"Content-Length": str(len(body))}, (body if method == "GET" else b"")


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Point publish at a temporary catalogue and a fake live site; capture say() and die()."""
    path = tmp_path / "catalogue.json"
    monkeypatch.setattr(publish, "CATALOGUE", str(path))
    monkeypatch.setattr(publish, "origin", lambda: BASE)
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    monkeypatch.setattr(publish, "run", lambda *a, **k: 0)          # the railway-logs hint on a timeout
    out = []
    monkeypatch.setattr(publish, "say", lambda m="": out.append(str(m)))

    def die(msg):
        out.append("DIE " + str(msg))
        raise SystemExit(1)
    monkeypatch.setattr(publish, "die", die)

    def use(local, linux=LINUX):
        path.write_text(json.dumps(local, indent=2), encoding="utf-8")
        fake = FakeSite(local, linux)
        monkeypatch.setattr(publish, "http", fake.http)
        return fake
    use.out = out
    return use


def died(out):
    return [line for line in out if line.startswith("DIE ")]


def test_matching_bytes_pass_and_every_download_is_hashed(site):
    fake = site(catalogue())
    publish.verify(wait_seconds=0)
    assert not died(site.out)
    gets = {url for method, url in fake.requests if method == "GET"}
    heads = {url for method, url in fake.requests if method == "HEAD"}
    downloads = {BASE + "/hub/Setup-9.9.9.exe", BASE + "/packs/BB5-1.0.0.zip",
                 BASE + "/hub/LightsOut-Linux-Native-9.9.9-x86_64.zip",
                 BASE + "/hub/LightsOut-Linux-Native-9.9.9-source.zip"}
    assert downloads <= heads and downloads <= gets
    assert "Live site verified." in site.out
    assert any(line.startswith("  catalogue.linux.update: signed with key " + trust.key_id(ed25519_sign(KEY, b"")[0]))
               for line in site.out)


def test_same_length_different_build_dies(site):
    fake = site(catalogue())
    fake.files[BASE + "/hub/Setup-9.9.9.exe"] = IMPOSTOR
    with pytest.raises(SystemExit):
        publish.verify(wait_seconds=0)
    assert "NOT the hub" in died(site.out)[0]


def test_wrong_length_dies(site):
    fake = site(catalogue())
    fake.files[BASE + "/hub/Setup-9.9.9.exe"] = HUB[:-10]
    with pytest.raises(SystemExit):
        publish.verify(wait_seconds=0)
    assert "expected %d" % len(HUB) in died(site.out)[0]


def test_an_entry_without_sha256_passes_but_says_so(site):
    site(catalogue(hub_sha=False))
    publish.verify(wait_seconds=0)
    assert any("bytes unverified" in line for line in site.out)


@pytest.mark.parametrize("which", ["x86_64", "source"])
@pytest.mark.parametrize("change", ["same length", "wrong length", "missing"])
def test_a_wrong_linux_download_dies(site, which, change):
    fake = site(catalogue())
    url = BASE + "/hub/LightsOut-Linux-Native-9.9.9-%s.zip" % which
    good = fake.files[url]
    if change == "same length":
        fake.files[url] = good[:-1] + b"X"
    elif change == "wrong length":
        fake.files[url] = good + b"X"
    else:
        del fake.files[url]
    with pytest.raises(SystemExit):
        publish.verify(wait_seconds=0)
    message = died(site.out)[0]
    assert url in message
    if change == "same length":
        assert "NOT the Linux %s" % ("ZIP" if which == "x86_64" else "source ZIP") in message


@pytest.mark.parametrize("field", ["sha256", "size", "download_url", "source_sha256", "source_size", "source_url"])
def test_an_incomplete_linux_entry_dies_instead_of_skipping(site, field):
    local = catalogue()
    del local["linux"][field]
    site(local)
    with pytest.raises(SystemExit):
        publish.verify(wait_seconds=0)
    assert "catalogue.linux is incomplete" in died(site.out)[0]


def test_no_linux_entry_means_no_linux_request(site):
    fake = site(catalogue(linux=False))
    publish.verify(wait_seconds=0)
    assert not died(site.out)
    assert not any("Linux" in url for _m, url in fake.requests)


def other_zip_signed_by_key():
    """A served ZIP that pins OTHER only, with a record KEY signed: clients of that ZIP refuse it."""
    body = linux_zip(OTHER)
    return body, update_record(body)


@pytest.mark.parametrize("case,reason", [
    ("unpinned key", "its key_id is '%s', and the served ZIP pins only" % trust.key_id(ed25519_sign(KEY, b"")[0])),
    ("signature by another key", "the Linux client refuses it under key"),
    ("record of another build", "the Linux client refuses it under key"),
    ("no update_trust_linux.py", "the served Linux ZIP lacks support/runtime/hub/update_trust_linux.py"),
    ("not a zip", "the served Linux ZIP is not a readable ZIP"),
])
def test_a_live_update_record_the_client_refuses_dies(site, case, reason):
    body = LINUX
    if case == "unpinned key":
        body, update = other_zip_signed_by_key()
    elif case == "signature by another key":
        update = update_record(signer=OTHER, key_of=KEY)
    elif case == "record of another build":
        update = update_record(archive_sha="0" * 64)
    elif case == "not a zip":
        body = b"PRETEND-LINUX-ZIP-" + b"L" * 300
        update = update_record(body)
    else:
        body = linux_zip(KEY, trust_member=False)
        update = update_record(body)
    site(catalogue(linux_zip_bytes=body, update=update), body)
    with pytest.raises(SystemExit):
        publish.verify(wait_seconds=0)
    message = died(site.out)[0]
    assert "the live catalogue.linux.update would never be offered" in message and reason in message, message


def test_a_live_catalogue_that_differs_is_not_accepted(site):
    fake = site(catalogue())
    live = catalogue()
    live["linux"]["version"] = "9.9.8"
    fake.files[BASE + "/catalogue.json"] = json.dumps(live).encode()
    with pytest.raises(SystemExit):
        publish.verify(wait_seconds=0)
    assert "did not match the local one" in died(site.out)[0]


if __name__ == "__main__":
    sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", __file__]))
