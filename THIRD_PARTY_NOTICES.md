# Third-party notices

The root MIT license applies to original Lights Out code and documentation.
It does not relicense the following material or the dependencies it uses.

## Bundled fonts

Oswald: Copyright 2016 The Oswald Project Authors. IBM Plex Sans: Copyright 2017
IBM Corp., with Reserved Font Name "Plex". Both use SIL Open Font License 1.1.
The Latin and Latin Extended WOFF2 subsets appear in `hub/webui/static/fonts/`
and `server/public/assets/`. Copies of the notices accompany the fonts and are
also retained in [LICENSES/](LICENSES/).

Upstream notices:

- <https://github.com/google/fonts/blob/main/ofl/oswald/OFL.txt>
- <https://github.com/google/fonts/blob/main/ofl/ibmplexsans/OFL.txt>

## CityHash

`tools/pak/cityhash.py` implements Google's CityHash algorithm in Python.
The Google copyright and MIT notice are retained in
[LICENSES/CityHash-MIT.txt](LICENSES/CityHash-MIT.txt).
Upstream: <https://github.com/google/cityhash>.

## Dependencies and development tools

The optional Rust helper declares `oozextract = "=0.5.0"`, whose
[upstream crate metadata](https://docs.rs/crate/oozextract/0.5.0/source/Cargo.toml.orig)
declares MIT. The dependency is fetched by Cargo, not vendored here; its
transitive dependencies retain their own terms.

**pyooz 0.0.8 is GPLv3 or later**, as declared by its
[published package](https://pypi.org/project/pyooz/0.0.8/). It is installed as a
dependency and is bundled by the existing desktop packaging specification.
The original Lights Out source files remain MIT; this does not make a combined
binary containing pyooz MIT-only. Before redistributing such a binary, satisfy
the applicable GPL terms, including corresponding-source and notice requirements,
or choose a compatible alternative. This source release does not attach or
relicense the official Windows installer. The versioned upstream source archive
is linked from the package page above.

The server uses Nodemailer (MIT-0), pinned in `server/package-lock.json`; it is
installed through npm rather than vendored. Account tests install fakeredis
with Lua support and its dependencies under their respective licenses.

Python, Node.js, pyooz/ooz, oozextract, Pillow, pystray, pywebview, pythonnet,
PyInstaller, Inno Setup, Unreal Engine and WebView2 have their own license terms.
Most are installed separately and are not vendored in this source snapshot.
`hub/requirements.txt` and `tools/pak/oozcli/Cargo.toml` identify direct
dependencies. A binary distributor must retain notices and satisfy the terms of
all included dependencies, including any source-availability obligations.

## Omitted material

Third-party click/report/map-ban sound recordings are not distributed here
because their source licenses were not recorded in the project. Game assets,
extracted game headers, generated game/plugin stubs, engine binaries and private
operational data are also omitted. No MIT license is claimed for those materials.

Bodycam and its assets belong to their respective owners. This project is not
affiliated with or endorsed by Reissad Studio.
