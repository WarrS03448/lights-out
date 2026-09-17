"""Isolated compile probe. Creates unsaved collector-only assets; no installed game access.

Run UE Editor-Cmd -run=pythonscript -script=<this path> -nullrhi -unattended.
This intentionally does NOT save a stripped GM_BB5 over the integrated asset.
"""
import os
import sys
import unreal

sys.path.insert(0, os.path.dirname(__file__))
import combat_graphs as C

T = unreal.BodycamMirrorTools
out = os.path.join(unreal.Paths.project_dir(), 'combat_compile_summary.txt')
open(out, 'w').close()


def log(s):
    with open(out, 'a', encoding='utf8') as f: f.write(s + '\n')
    unreal.log(s)


def compile(bp):
    report = T.compile_and_report(bp)
    log(bp.get_name() + ': ' + str(report))
    if 'ERROR:' in report or 'STATUS: BS_Error' in report: raise RuntimeError(report)


def build(bp, graph, fn):
    report = T.build_graph(bp, graph, fn()); log(fn.__name__ + ': ' + report)
    if not report.splitlines()[0].endswith(' 0 error(s)'): raise RuntimeError(report)


def create(path, name, parent):
    # Compile in a transient package so an existing integrated asset is never deleted.
    factory = unreal.BlueprintFactory(); factory.set_editor_property('parent_class', parent)
    bp = unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, path, unreal.Blueprint, factory)
    if not bp: raise RuntimeError('Cannot create ' + name + '; use clean probe paths')
    compile(bp); return bp


def variables(bp, specs):
    for name, category, cls, container in specs:
        sub = unreal.load_class(None, cls) if cls else None
        if not T.add_variable(bp, name, category, sub, container == 'array'): raise RuntimeError(name)
    compile(bp)


try:
    # Native reflection calls stay identical; only our two generated class paths vary.
    C.DIR = '/Game/CombatCompileProbe'
    C.GM = C.DIR + '/GM_CombatProbe.GM_CombatProbe_C'
    C.OBSERVER = C.DIR + '/BP_CombatProbe.BP_CombatProbe_C'
    C.OBSERVER_VARIABLES = [(n, t, C.GM if n == 'CombatGM' else c, a) for n, t, c, a in C.OBSERVER_VARIABLES]
    gm = create(C.DIR, 'GM_CombatProbe', unreal.Actor)
    obs = create(C.DIR, 'BP_CombatProbe', unreal.Actor)
    variables(gm, C.GM_VARIABLES); build(gm, 'EventGraph', C.gm_events)
    for name, signature, logic in C.FUNCTIONS: build(gm, name, signature)
    compile(gm)
    variables(obs, C.OBSERVER_VARIABLES); build(obs, 'EventGraph', C.observer_events); compile(obs)
    build(obs, 'EventGraph', C.observer_logic); compile(obs)
    for name, signature, logic in C.FUNCTIONS: build(gm, name, logic)
    compile(gm)
    build(gm, 'EventGraph', C.gm_logic); compile(gm)
    log('RESULT: OK')
except Exception:
    import traceback
    log(traceback.format_exc()); log('RESULT: FAILED')
