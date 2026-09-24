"""Equal-width URL migration for authored cooked assets; used by publish.py and recording.py."""
import hashlib
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def retarget_bytes(data, source_host, target_host):
    if source_host == target_host:
        raise ValueError('Endpoint hosts must be different')
    for host in (source_host, target_host):
        if not isinstance(host, str) or not re.fullmatch(r'[a-z0-9.-]+', host):
            raise ValueError('Endpoint host must be an ASCII hostname')
    old, new = source_host.encode('ascii'), target_host.encode('ascii')
    if len(old) != len(new):
        raise ValueError('Endpoint hosts must have the same byte length')
    if new in data:
        raise ValueError('Target endpoint already appears in the original asset')
    result = data.replace(old, new)
    if len(result) != len(data) or result.replace(new, old) != data:
        raise ValueError('Endpoint transformation changed unrelated bytes')
    return result


def retarget_tree(folder, source_host, target_host, proof, *, require_match=True):
    """Validate all assets in memory before writing any; headers and offsets stay fixed."""
    retarget_bytes(b'', source_host, target_host)
    sys.path.insert(0, str(ROOT / 'tools/pak'))
    import kismet
    pending = []
    for path in sorted(Path(folder).rglob('*')):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if source_host.encode('ascii') not in raw:
            continue
        if path.suffix != '.uexp':
            raise ValueError('Endpoint outside supported bytecode asset: ' + str(path))
        pkg = kismet.Pkg(str(path.with_suffix('.uasset')), str(path))
        before = {}
        count = 0
        for index, _ in kismet.list_functions(pkg):
            fn = kismet.parse_function(pkg, index)
            if source_host.encode('ascii') in fn['code']:
                lines = kismet.Dis(pkg, fn['code']).run()
                if any(source_host in line and ': String ' not in line for line in lines):
                    raise ValueError('Endpoint is not an inline string constant')
                count += sum(line.count(source_host) for line in lines)
                before[index] = lines
        if count != raw.count(source_host.encode('ascii')):
            raise ValueError('Not every endpoint is accounted for by disassembly')
        changed = retarget_bytes(raw, source_host, target_host)
        pkg.b = changed
        for index, lines in before.items():
            after = kismet.Dis(pkg, kismet.parse_function(pkg, index)['code']).run()
            if after != [line.replace(source_host, target_host) for line in lines]:
                raise ValueError('Bytecode differs beyond the endpoint literal')
        pending.append((path, changed, {'asset': path.relative_to(folder).as_posix(),
            'endpoints': count, 'bytes': len(raw),
            'source_sha256': hashlib.sha256(raw).hexdigest(),
            'target_sha256': hashlib.sha256(changed).hexdigest()}))
    if not pending and require_match:
        raise ValueError('No endpoint literals found in authored assets')
    for path, changed, evidence in pending:
        path.write_bytes(changed)
        proof.append(evidence)


def zip_tree(folder,target):
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(Path(folder).rglob('*')):
            if path.is_file():
                entry=zipfile.ZipInfo(path.relative_to(folder).as_posix(),(2020,1,1,0,0,0))
                entry.compress_type=zipfile.ZIP_DEFLATED
                entry.external_attr=0o644<<16
                z.writestr(entry,path.read_bytes())


def verify_proof(proof, migration=False, ranked_modes=("BB5",), recovery=False):
    from collections import Counter
    modes = tuple(ranked_modes)
    if not modes or len(set(modes)) != len(modes) or any(m not in ("BB5", "BB1") for m in modes):
        raise ValueError("Unexpected ranked endpoint inventory")
    lobby = 'Bodycam/Content/GM/Gamemode/'
    expected = Counter({(lobby+"GM_CHJoin.uexp", 1): 1, (lobby+"GM_CHLobby.uexp", 3): 1})
    for mode in modes:
        def asset(name):
            return 'cooked/'+lobby+('' if name==f'GM_{mode}.uexp' else mode+'/')+name
        for name, count in ((f"GM_{mode}.uexp", 16), (f"BP_{mode}StartRequest.uexp", 1),
                            (f"BP_{mode}TeamRequest.uexp", 9), ("BP_CHCombatRequest.uexp", 1)):
            expected[(asset(name), count)] += 1
        if migration:
            expected[(asset(f"BP_{mode}MigrationRequest.uexp"), 1)] += 1
        if recovery:
            expected[(asset(f"BP_{mode}Recovery.uexp"), 2)] += 1
            expected[(asset(f"BP_{mode}RestoreRequest.uexp"), 1)] += 1
    actual = Counter((item["asset"], item["endpoints"]) for item in proof)
    if actual != expected:
        raise ValueError("Authored endpoint baseline changed; review all authored assets before building")
