"""Constants for Lights Out.

Everything here is a plain constant so the rest of the package (and a PyInstaller
spec) can import it without pulling in tkinter or anything else. The identity block is
also what hub.spec stamps into the exe's version-info resource (CompanyName, ProductName,
FileDescription, LegalCopyright) and what installer.iss shows in Add/Remove Programs.

Environment overrides are read once, at import time:
  HUB_CATALOGUE_URL   use a different catalogue.json (local server, staging, tests)
  HUB_API_BASE        point sign-in / the competitive API somewhere else (a local server, tests)
  HUB_GAME_DIR        skip Steam detection and use this folder as <GameDir>
  HUB_STATE_DIR       put packs/, work/, logs/ and state.json somewhere else
"""
import os

# ---------------------------------------------------------------- identity
APP_NAME = "Lights Out"
HUB_VERSION = "2.6.2"                       # display version: shown in the UI, the installer name, AppVersion
# Version-info resource fields (hub.spec). Plain strings; the exe's file version is HUB_VERSION.
COMPANY_NAME = "Lights Out (unofficial)"
PRODUCT_NAME = APP_NAME
FILE_DESCRIPTION = "Lights Out (unofficial Bodycam community client)"
# \u00a9 (the \u00a9 sign) is written as an escape on purpose so the string survives an editor or tool
# re-encoding this file to a non-UTF-8 codepage; it becomes a real \u00a9 in the UTF-16LE resource.
LEGAL_COPYRIGHT = "\u00a9 2026 Lights Out contributors. Not affiliated with Reissad Studio."


def version_tuple(v=HUB_VERSION):
    """HUB_VERSION -> a four-int tuple for a Windows version resource / installer.

    '1.1.0' -> (1, 1, 0, 0); a pre-release tag like '1.2.0-beta' -> (1, 2, 0, 0). Each dotted part
    contributes its leading digits (non-numeric suffix dropped), padded/truncated to exactly four."""
    nums = []
    for part in str(v).split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        nums.append(int(digits) if digits else 0)
        if len(nums) == 4:
            break
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums[:4])


# Numeric "a.b.c.d" form. hub.spec uses it for the file/product version fields (they must be numeric,
# so a '-beta' tag would crash the resource builder) and build_hub.bat passes it to Inno Setup as
# VersionInfoVersion. AppVersion / the installer filename keep the display string HUB_VERSION.
HUB_FILE_VERSION = ".".join(str(n) for n in version_tuple())

# Code-signing identity. Kept separate from COMPANY_NAME above because it is the name that has to
# match the certificate, not the name shown in the version resource. docs/code-signing.md: whatever
# identity Azure validates becomes the publisher Windows shows, and PUBLISHER must equal it exactly.
#
# The 1.1.0 merge (2026-09-14) dropped the duplicate FILE_DESCRIPTION and COPYRIGHT that used to sit
# here: the installer branch renamed them to FILE_DESCRIPTION / LEGAL_COPYRIGHT at the top of this
# file, and because both definitions survived the merge the later pair silently won, so the exe would
# have carried these strings instead of the intended ones. The top of the file is the only definition.
PUBLISHER = "Samuel Warren"

# ---------------------------------------------------------------- endpoints / file names
DEFAULT_CATALOGUE_URL = "https://lightsout.up.railway.app/catalogue.json"
# The API the Competitive tab talks to (sign-in today; the match backend next). Derived from
# the catalogue URL on purpose, so the hub's copy of the domain lives in ONE place.
#
# It is NOT the only copy in the project, and 2026-09-16 is why this warning exists. The Lights
# Out rename RENAMED the Railway domain instead of adding one, the old host started returning
# Railway's edge 404 ("Application not found", x-railway-fallback: true), and every installed hub
# sat on "Lost the connection to the competitive service. Reconnecting..." forever - with no way
# out, because the catalogue it would have updated from is on the same dead host.
#
# Changing the domain means changing, at minimum: this file, server/public/catalogue.json (its
# absolute download_url / pack_url / page_url), hub/installer.iss, the tools/ probe watchers, and
# mirror/Bodycam/Scripts/*_graphs.py - the last of which is BAKED INTO A COOKED PAK, so the
# installed gamemode keeps calling the old host until the pak is rebuilt and republished.
# Prefer ADDING a domain in Railway and retiring the old one later over renaming one.
DEFAULT_API_BASE = DEFAULT_CATALOGUE_URL.rsplit("/", 1)[0]
PAK_NAME = "CommunityGamemodes_P.pak"      # the single merged pak we install into ~mods
GAME_EXE = "Bodycam-Win64-Shipping.exe"    # used to tell whether the game is running

# ---------------------------------------------------------------- environment overrides
# Read at import. paths.state_dir() / game.find_game_dir() re-read os.environ at call
# time as well, so a test that sets the variable after import still works; these values
# are the fallback for the normal case.
CATALOGUE_URL = os.environ.get("HUB_CATALOGUE_URL") or DEFAULT_CATALOGUE_URL
# NB: not derived from CATALOGUE_URL, because --local points that at a file:// path while
# sign-in must still reach the real service.
API_BASE = os.environ.get("HUB_API_BASE") or DEFAULT_API_BASE
GAME_DIR_OVERRIDE = os.environ.get("HUB_GAME_DIR") or None
STATE_DIR_OVERRIDE = os.environ.get("HUB_STATE_DIR") or None
