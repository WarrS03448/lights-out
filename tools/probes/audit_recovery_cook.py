"""Run: python tools/probes/audit_recovery_cook.py --out work/recovery-candidate/audit.

Disassemble authored recovery dependencies and retain UTF-8 evidence. This checks
compiled data flow, not actual Bodycam behavior or Steam host recovery.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools/pak'))
import kismet


def audit(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    summary=(ROOT/'mirror/Bodycam/blueprints_summary.txt').read_text(encoding='utf-8')
    if not summary.rstrip().endswith('RESULT: OK') or re.search(r'\b(?:ERROR|WARNING)\b',summary):
        raise ValueError('Blueprint verdict is not clean')
    cooked=ROOT/'mirror/Bodycam/Saved/Cooked/Windows/Bodycam/Content/GM/Gamemode'
    paths=['GM_CHLobby','GM_CHJoin']
    for mode in ('BB5','BB1'):
        paths += ['GM_'+mode]+[mode+'/'+name for name in (
            'AC_'+mode+'BombRule','BP_'+mode+'Recovery','BP_'+mode+'RestoreRequest',
            'BP_'+mode+'MigrationRequest','BP_'+mode+'StartRequest','BP_'+mode+'TeamRequest','BP_CHCombatRequest')]
    evidence=[]
    for name in paths:
        base=cooked/name;ua=base.with_suffix('.uasset');ue=base.with_suffix('.uexp')
        package=kismet.Pkg(str(ua),str(ue));functions={}
        for index,fn_name in kismet.list_functions(package):
            fn=kismet.parse_function(package,index)
            functions[fn_name]='\n'.join(kismet.Dis(package,fn['code']).run())
        text='\n\n'.join('===== '+n+' =====\n'+code for n,code in functions.items())
        if name.endswith('Recovery'):
            restore=functions['ApplySnapshot']
            for prop in ('CurrentRound','ObjectiveTeam','CounterObjectiveTeam','TeamID','Kill','Death','SpawnCount'):
                if not re.search(r'\$Instance '+prop+r'@[^\n]*\n\s+[0-9a-f]+: \$Local ',restore):
                    raise ValueError(name+': restore value is not linked: '+prop)
            for required in ('Array_Set@','RetrievePlatformIdAsStringFromPlayerState@','ch_session_pulse','ch_recovery_checkpoint'):
                if required not in text:raise ValueError(name+': missing '+required)
            for required in ('GetRealTimeSeconds@','RestoreDeadline@','SeedSession@','LocalVirtualFunction MatchExit','Int 720'):
                if required not in text:raise ValueError(name+': missing bounded recovery exit '+required)
        if name.endswith('RestoreRequest'):
            for required in ('ch_recovery_prepared','ch_recovery_restored','StartCountdown@','BuildSnapshot','HostEpoch@','Consumed@','Game.Phase.RoundWarmup','GetCurrentPhase@'):
                if required not in text:raise ValueError(name+': missing '+required)
            approved=re.findall(r'^\w+: Let \(StartApprovedHumans@[^\n]*\n(?:[ \t]+[^\n]*\n)+',text,re.M)
            if len(approved)!=2 or any('$Instance SnapshotIds@' not in block for block in approved) or 'CallFinal CaptureStartHumans@' in text:
                raise ValueError(name+': approved roster must come from the verified snapshot')
        if name=='GM_CHLobby' and any('CallMath '+fn+'@' in text for fn in ('FindLobbies','JoinLobby')):
            raise ValueError('Host lobby must not run a parallel native search')
        (output/(name.replace('/','-')+'.txt')).write_text(text,encoding='utf-8')
        evidence.append({'asset':name,'functions':list(functions),'files':{
            p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ua,ue)}})
    (output/'manifest.json').write_text(json.dumps({'native_game_verified':False,'assets':evidence},indent=2),encoding='utf-8')
    print(f'{len(paths)} authored assets disassembled; restore fields are linked. Native recovery remains unverified.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);args=p.parse_args();audit(args.out)
