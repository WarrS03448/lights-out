"""Passive, host-only combat observer. Shipping behaviour still requires a private-match probe.

One actor per current player pawn retains the GAS async action and survivor binding.
A manager actor owns sequence/epoch and a bounded 256-row queue drained at up to 5 Hz.
Missing binds, transport errors and queue overflow are explicit; eligibility stays server gated.
No cost-effect/shot inference, damage commands, or automatic health/hit joins.
"""
from ctf_graphs import G, P, SYS, MATH, STR, GS_LIB, ARR, ACTOR, PAWN, CONTROLLER, BC_PS, BC_GS

DIR = '/Game/GM/Gamemode/BB5'
OBSERVER = DIR + '/BP_CHCombatObserver.BP_CHCombatObserver_C'
GM = DIR + '/BP_CHCombatManager.BP_CHCombatManager_C'
SURVIVOR = '/Script/Bodycam.BodycamSurvivorComponent'
ASYNC = '/Script/GameplayAbilities.AbilityAsync_WaitGameplayEffectApplied'
ASYNC_BASE = '/Script/GameplayAbilities.AbilityAsync'
GAS = '/Script/GameplayAbilities.AbilitySystemBlueprintLibrary'
PS = '/Script/Engine.PlayerState'
GS = '/Script/Engine.GameStateBase'
COMP = '/Script/Engine.ActorComponent'
ONLINE = '/Script/Bodycam.BodycamOnlineManager'
TAG = '/Script/GameplayTags.BlueprintGameplayTagLibrary'
GUID = '/Script/Engine.KismetGuidLibrary'
QUEUE_LIMIT = 256

# Tuples consumed by make_blueprints.add_var(pin(category, loaded subobject, container)).
GM_VARIABLES = [
    ('CombatSeq', 'int', None, None), ('CombatEpoch', 'string', None, None),
    ('CombatQueue', 'string', None, 'array'), ('CombatGaps', 'int', None, None),
    ('CombatBound', 'int', None, None), ('CombatFound', 'bool', None, None),
    ('CombatResolvedState', 'object', PS, None),
    ('CombatActive', 'bool', None, None), ('CombatClosed', 'bool', None, None),
    ('CombatInFlight', 'bool', None, None),
    ('CombatCausers', 'object', ACTOR, 'array'),
    ('CombatBoundary', 'bool', None, None),
]
OBSERVER_VARIABLES = [
    ('CombatPawn', 'object', PAWN, None), ('CombatGM', 'object', GM, None),
    ('CombatSurvivor', 'object', SURVIVOR, None), ('CombatAction', 'object', ASYNC_BASE, None),
    ('CombatASC', 'object', '/Script/GameplayAbilities.AbilitySystemComponent', None),
    ('CombatReady', 'bool', None, None),
]


def _valid(g, name, pin):
    g.call(name, SYS, 'IsValid'); g.link((pin, name + '.Object'))
    return name + '.ReturnValue'


def _string(g, name, pin, kind='Int'):
    g.call(name, STR, 'Conv_' + kind + 'ToString'); g.link((pin, name + '.In' + kind))
    return name + '.ReturnValue'


def _row(g, prefix, fields):
    """fields = [(literal, optional dynamic string pin)]; only controlled IDs reach the wire."""
    last = None
    for i, (literal, pin) in enumerate(fields):
        name = prefix + str(i)
        g.call(name, STR, 'Concat_StrStr', {'B': literal} if last else {'A': literal})
        if last: g.link((last, name + '.A'))
        if not last and pin: g.link((pin, name + '.B'))
        last = name + '.ReturnValue'
        if i and pin:
            g.call(name + 'v', STR, 'Concat_StrStr'); g.link((last, name + 'v.A'), (pin, name + 'v.B'))
            last = name + 'v.ReturnValue'
    return last


def _optional(g, name, key, value):
    """Omit unavailable wire fields instead of serializing empty or zero values."""
    g.call(name + 'len', STR, 'Len'); g.link((value, name + 'len.S'))
    g.call(name + 'present', MATH, 'Greater_IntInt', {'B': '0'}); g.link((name + 'len.ReturnValue', name + 'present.A'))
    g.call(name + 'bounded', MATH, 'LessEqual_IntInt', {'B': '128'}); g.link((name + 'len.ReturnValue', name + 'bounded.A'))
    g.call(name + 'known', STR, 'NotEqual_StriStri', {'B': 'None'}); g.link((value, name + 'known.A'))
    g.call(name + 'lengthok', MATH, 'BooleanAND'); g.link((name + 'present.ReturnValue', name + 'lengthok.A'), (name + 'bounded.ReturnValue', name + 'lengthok.B'))
    g.call(name + 'include', MATH, 'BooleanAND'); g.link((name + 'lengthok.ReturnValue', name + 'include.A'), (name + 'known.ReturnValue', name + 'include.B'))
    g.call(name + 'pair', STR, 'Concat_StrStr', {'A': ';' + key + '='}); g.link((value, name + 'pair.B'))
    g.call(name, MATH, 'SelectString', {'B': ''})
    g.link((name + 'include.ReturnValue', name + '.bPickA'), (name + 'pair.ReturnValue', name + '.A'))
    return name + '.ReturnValue'


def _identity_fields(g, prefix, source, target):
    return [('', _optional(g, prefix + key, key, pin)) for key, pin in
            [('a', source + '.ID'), ('b', target + '.ID'), ('at', source + '.Team'), ('bt', target + '.Team')]]


def _inc(g, prefix, var, after):
    g.get(prefix + 'get', var); g.call(prefix + 'add', MATH, 'Add_IntInt', {'B': '1'})
    g.set(prefix + 'set', var)
    g.link((prefix + 'get.' + var, prefix + 'add.A'), (prefix + 'add.ReturnValue', prefix + 'set.' + var), (after, prefix + 'set.exec'))
    return prefix + 'set.then'


def gm_events():
    g = G()
    for name in ('CombatStart', 'CombatReconcile', 'CombatFlush', 'CombatCoverage', 'CombatRoundStarted', 'CombatClose'):
        g.custom(name, name)
    g.custom('emit', 'CombatEmit', [P('Row', 'string')])
    g.custom('batchreply', 'CombatBatchReply', [P('Attempt', 'int'), P('Success', 'bool')])
    return g.json()


def resolve_signature():
    g = G(); g.entry(params=[P('Subject', 'object', **{'class': ACTOR})])
    g.result(params=[P('ID', 'string'), P('Team', 'string')]); return g.json()


def resolve_logic():
    """Recognize only explicit Pawn, Controller or PlayerState associations; null stays unknown."""
    g = G(); g.entry(); g.result()
    g.set('clear', 'CombatResolvedState'); g.chain('entry', 'clear')
    g.cast('pawn', PAWN); g.link(('entry.Subject', 'pawn.cast_object')); g.chain('clear', 'pawn')
    g.get('pps', 'PlayerState', PAWN); g.link(('pawn.cast_result', 'pps.self'))
    g.set('frompawn', 'CombatResolvedState'); g.link(('pps.PlayerState', 'frompawn.CombatResolvedState'), ('pawn.cast_ok', 'frompawn.exec'))
    g.cast('controller', CONTROLLER); g.link(('entry.Subject', 'controller.cast_object'), ('pawn.cast_fail', 'controller.exec'))
    g.get('cps', 'PlayerState', CONTROLLER); g.link(('controller.cast_result', 'cps.self'))
    g.set('fromcontroller', 'CombatResolvedState'); g.link(('cps.PlayerState', 'fromcontroller.CombatResolvedState'), ('controller.cast_ok', 'fromcontroller.exec'))
    g.cast('ps', PS); g.link(('entry.Subject', 'ps.cast_object'), ('controller.cast_fail', 'ps.exec'))
    g.set('fromstate', 'CombatResolvedState'); g.link(('ps.cast_result', 'fromstate.CombatResolvedState'), ('ps.cast_ok', 'fromstate.exec'))
    g.get('resolved', 'CombatResolvedState'); g.branch('known')
    g.link((_valid(g, 'valid', 'resolved.CombatResolvedState'), 'known.condition'))
    for node in ('frompawn', 'fromcontroller', 'fromstate'): g.chain(node, 'known')
    g.link(('ps.cast_fail', 'result.exec'), ('known.else', 'result.exec'))
    g.call('id', ONLINE, 'RetrievePlatformIdAsStringFromPlayerState'); g.link(('resolved.CombatResolvedState', 'id.PlayerState'))
    g.call('idlength', STR, 'Len'); g.link(('id.ReturnValue', 'idlength.S'))
    g.call('sidlength', MATH, 'EqualEqual_IntInt', {'B': '17'}); g.link(('idlength.ReturnValue', 'sidlength.A'))
    g.call('idnumeric', STR, 'IsNumeric'); g.link(('id.ReturnValue', 'idnumeric.SourceString'))
    g.call('sidvalid', MATH, 'BooleanAND'); g.link(('sidlength.ReturnValue', 'sidvalid.A'), ('idnumeric.ReturnValue', 'sidvalid.B'))
    g.call('sidvalue', MATH, 'SelectString', {'B': ''}); g.link(('sidvalid.ReturnValue', 'sidvalue.bPickA'), ('id.ReturnValue', 'sidvalue.A'))
    g.cast('body', BC_PS); g.link(('known.then', 'body.exec'), ('resolved.CombatResolvedState', 'body.cast_object'))
    g.get('team', 'TeamID', BC_PS); g.link(('body.cast_result', 'team.self'))
    # Separate result node copies are needed for unknown to remain blank, not ID/team zero.
    g.n('known_result', 'functionresult')
    g.call('teamknown', MATH, 'GreaterEqual_IntInt', {'B': '0'}); g.link(('team.TeamID', 'teamknown.A'))
    g.call('teamvalue', MATH, 'SelectString', {'B': ''}); g.link(('teamknown.ReturnValue', 'teamvalue.bPickA'), (_string(g, 'teamstr', 'team.TeamID'), 'teamvalue.A'))
    g.link(('body.cast_ok', 'known_result.exec'), ('body.cast_fail', 'result.exec'), ('sidvalue.ReturnValue', 'known_result.ID'),
           ('teamvalue.ReturnValue', 'known_result.Team'))
    return g.json()


def observer_events():
    g = G()
    g.custom('start', 'CombatObserve', [P('Pawn', 'object', **{'class': PAWN}), P('Manager', 'object', **{'class': GM})])
    g.custom('health', 'CombatHealth', [P('HealthComponent', 'object', **{'class': SURVIVOR}), P('OldValue', 'float32'),
                                      P('NewValue', 'float32'), P('Instigator', 'object', **{'class': ACTOR})])
    g.custom('check', 'CombatCheck')
    return g.json()


def causer_signature():
    g = G(); g.entry(params=[P('Subject', 'object', **{'class': ACTOR})]); g.result(params=[P('ID', 'string')]); return g.json()


def causer_logic():
    """Monotonic per-epoch actor registry; actor names are reusable and not incident IDs."""
    g = G(); g.entry(); g.result(); g.get('actors', 'CombatCausers'); g.get('epoch', 'CombatEpoch')
    g.call('find', ARR, 'Array_Find', array=True); g.link(('actors.CombatCausers', 'find.TargetArray'), ('entry.Subject', 'find.ItemToFind'))
    g.call('existing', MATH, 'GreaterEqual_IntInt', {'B': '0'}); g.link(('find.ReturnValue', 'existing.A'))
    g.branch('known'); g.link(('existing.ReturnValue', 'known.condition')); g.chain('entry', 'known')
    g.call('length', ARR, 'Array_Length', array=True); g.link(('actors.CombatCausers', 'length.TargetArray'))
    g.call('capacity', MATH, 'Less_IntInt', {'B': '1024'}); g.link(('length.ReturnValue', 'capacity.A'))
    g.branch('room'); g.link(('known.else', 'room.exec'), ('capacity.ReturnValue', 'room.condition'))
    g.call('add', ARR, 'Array_Add', array=True); g.link(('actors.CombatCausers', 'add.TargetArray'), ('entry.Subject', 'add.NewItem'), ('room.then', 'add.exec'))
    after = _inc(g, 'overflow', 'CombatGaps', 'room.else'); g.link((after, 'result.exec'))
    for name, index, after in [('found', 'find.ReturnValue', 'known.then'), ('new', 'add.ReturnValue', 'add.then')]:
        g.n(name + 'result', 'functionresult')
        row = _row(g, name + 'id', [('', 'epoch.CombatEpoch'), ('-', _string(g, name + 'index', index))])
        g.link((row, name + 'result.ID'), (after, name + 'result.exec'))
    return g.json()


def observer_logic():
    g = G(); g.existing('start', 'CombatObserve'); g.existing('health', 'CombatHealth'); g.existing('check', 'CombatCheck')
    g.set('pawnset', 'CombatPawn'); g.set('gmset', 'CombatGM')
    g.link(('start.Pawn', 'pawnset.CombatPawn'), ('start.Manager', 'gmset.CombatGM')); g.chain('start', 'pawnset', 'gmset')
    g.get('pawn', 'CombatPawn'); g.get('manager', 'CombatGM'); g.get('survivor', 'CombatSurvivor'); g.get('action', 'CombatAction')
    g.call('findhealth', SURVIVOR, 'FindBodycamSurvivorComponent'); g.link(('pawn.CombatPawn', 'findhealth.Actor'))
    g.set('healthset', 'CombatSurvivor'); g.link(('findhealth.ReturnValue', 'healthset.CombatSurvivor')); g.chain('gmset', 'healthset')
    # Liveness timer is armed even on missing components. Reconcile replaces a failed observer on next pass.
    g.call('timer', SYS, 'K2_SetTimer', {'FunctionName': 'CombatCheck', 'Time': '0.5', 'bLooping': 'true'})
    g.selfnode('self'); g.link(('self.self', 'timer.Object')); g.chain('healthset', 'timer')
    g.branch('hashealth'); g.link((_valid(g, 'healthvalid', 'survivor.CombatSurvivor'), 'hashealth.condition')); g.chain('timer', 'hashealth')
    g.n('health_event', 'createevent', func='CombatHealth')
    g.adddelegate('health_bind', 'OnHealthChanged', SURVIVOR)
    g.link(('survivor.CombatSurvivor', 'health_bind.self'), ('health_event.OutputDelegate', 'health_bind.Delegate'), ('hashealth.then', 'health_bind.exec'))
    g.call('asc', GAS, 'GetAbilitySystemComponent'); g.link(('pawn.CombatPawn', 'asc.Actor'))
    g.set('retainasc', 'CombatASC'); g.link(('asc.ReturnValue', 'retainasc.CombatASC'))
    g.branch('hasasc'); g.link((_valid(g, 'ascvalid', 'asc.ReturnValue'), 'hasasc.condition')); g.chain('health_bind', 'retainasc', 'hasasc')
    g.n('effect', 'async', **{'class': ASYNC, 'func': 'WaitGameplayEffectAppliedToActor', 'defaults': {'TriggerOnce': 'false', 'ListenForPeriodicEffect': 'true'}})
    g.link(('pawn.CombatPawn', 'effect.TargetActor'), ('hasasc.then', 'effect.exec'))
    g.set('retain', 'CombatAction'); g.link(('effect.AsyncAction', 'retain.CombatAction')); g.chain('effect', 'retain')
    g.set('ready', 'CombatReady', defaults={'CombatReady': 'true'}); g.chain('retain', 'ready')

    # Health is authoritative observed health loss. Healing/reset/increases do not add damage.
    g.call('loss', MATH, 'Greater_DoubleDouble'); g.link(('health.OldValue', 'loss.A'), ('health.NewValue', 'loss.B'))
    g.branch('loss_only'); g.link(('loss.ReturnValue', 'loss_only.condition')); g.chain('health', 'loss_only')
    g.call('htarget', GM, 'ResolveCombatActor'); g.call('hsource', GM, 'ResolveCombatActor')
    for n in ('htarget', 'hsource'): g.link(('manager.CombatGM', n + '.self'))
    g.link(('pawn.CombatPawn', 'htarget.Subject'), ('health.Instigator', 'hsource.Subject'), ('loss_only.then', 'htarget.exec')); g.chain('htarget', 'hsource')
    g.call('maximum', SURVIVOR, 'GetMaxHealth'); g.link(('health.HealthComponent', 'maximum.self'))
    hrow = _row(g, 'hr', [('kind=health', None)] + _identity_fields(g, 'hi', 'hsource', 'htarget') + [
                       (';old=', _string(g, 'old', 'health.OldValue', 'Double')), (';new=', _string(g, 'new', 'health.NewValue', 'Double')),
                       (';max=', _string(g, 'max', 'maximum.ReturnValue', 'Double'))])
    g.call('healthsend', GM, 'CombatEmit'); g.link(('manager.CombatGM', 'healthsend.self'), (hrow, 'healthsend.Row')); g.chain('hsource', 'healthsend')

    # UE5.5 OnApplied.Source is the TARGET avatar. Resolve the source exclusively from context.
    g.call('context', GAS, 'GetEffectContext'); g.link(('effect.SpecHandle', 'context.SpecHandle'))
    g.call('source', GAS, 'EffectContextGetOriginalInstigatorActor'); g.link(('context.ReturnValue', 'source.EffectContext'))
    g.call('instigator', GAS, 'EffectContextGetInstigatorActor'); g.link(('context.ReturnValue', 'instigator.EffectContext'))
    g.call('sourceactor', MATH, 'SelectObject'); g.link(('source.ReturnValue', 'sourceactor.A'), ('instigator.ReturnValue', 'sourceactor.B'), (_valid(g, 'originalvalid', 'source.ReturnValue'), 'sourceactor.bSelectA'))
    g.call('targetid', GM, 'ResolveCombatActor'); g.call('sourceid', GM, 'ResolveCombatActor')
    for n in ('targetid', 'sourceid'): g.link(('manager.CombatGM', n + '.self'))
    g.cast('sourcecast', ACTOR, pure=True); g.link(('sourceactor.ReturnValue', 'sourcecast.cast_object'))
    g.link(('effect.OnApplied', 'context.exec'), ('pawn.CombatPawn', 'targetid.Subject'), ('sourcecast.cast_result', 'sourceid.Subject')); g.chain('context', 'targetid', 'sourceid')
    g.call('hashit', GAS, 'EffectContextHasHitResult'); g.link(('context.ReturnValue', 'hashit.EffectContext'))
    g.branch('actualhit'); g.link(('hashit.ReturnValue', 'actualhit.condition')); g.chain('sourceid', 'actualhit')
    g.call('hit', GAS, 'EffectContextGetHitResult'); g.link(('context.ReturnValue', 'hit.EffectContext'))
    g.call('breakhit', GS_LIB, 'BreakHitResult'); g.link(('hit.ReturnValue', 'breakhit.Hit'))
    hitrow = _row(g, 'hitrow', [('kind=hit', None)] + _identity_fields(g, 'ei', 'sourceid', 'targetid') + [
                             ('', _optional(g, 'bonefield', 'bone', _string(g, 'bone', 'breakhit.HitBoneName', 'Name'))), (';distance=', _string(g, 'distance', 'breakhit.Distance', 'Double'))])
    # Effect-causer class is optional source context; equipped inventory is deliberately not substituted.
    g.call('causer', GAS, 'EffectContextGetEffectCauser'); g.link(('context.ReturnValue', 'causer.EffectContext'))
    g.branch('hascauser'); g.link((_valid(g, 'causervalid', 'causer.ReturnValue'), 'hascauser.condition'), ('actualhit.then', 'hascauser.exec'))
    g.call('causerid', GM, 'ResolveCombatCauser'); g.link(('causer.ReturnValue', 'causerid.Subject'), ('manager.CombatGM', 'causerid.self'), ('hascauser.then', 'causerid.exec'))
    g.call('causerclass', '/Script/Engine.GameplayStatics', 'GetObjectClass'); g.link(('causer.ReturnValue', 'causerclass.Object'))
    g.call('classsoft', SYS, 'GetSoftClassPath'); g.link(('causerclass.ReturnValue', 'classsoft.Class'))
    g.call('classpath', SYS, 'BreakSoftClassPath'); g.link(('classsoft.ReturnValue', 'classpath.InSoftClassPath'))
    causerrow = _row(g, 'cr', [('', hitrow), ('', _optional(g, 'causerfield', 'causer', 'causerid.ID')), ('', _optional(g, 'weaponfield', 'weapon', 'classpath.PathString'))])
    for n, row, after in [('hitsend', hitrow, 'hascauser.else'), ('causersend', causerrow, 'causerid.then')]:
        g.call(n, GM, 'CombatEmit'); g.link(('manager.CombatGM', n + '.self'), (row, n + '.Row'), (after, n + '.exec'))

    # Stop on pawn replacement, destruction, missing bind, or lost GameMode. EndPlay handles travel too.
    g.get('isready', 'CombatReady'); g.branch('checkready'); g.link(('isready.CombatReady', 'checkready.condition')); g.chain('check', 'checkready')
    g.branch('pawnalive'); g.link((_valid(g, 'pawnvalid', 'pawn.CombatPawn'), 'pawnalive.condition'), ('checkready.then', 'pawnalive.exec'))
    g.get('ps', 'PlayerState', PAWN); g.link(('pawn.CombatPawn', 'ps.self'))
    g.branch('psalive'); g.link((_valid(g, 'psvalid', 'ps.PlayerState'), 'psalive.condition'), ('pawnalive.then', 'psalive.exec'))
    g.call('currentpawn', PS, 'GetPawn'); g.link(('ps.PlayerState', 'currentpawn.self'))
    g.call('samepawn', MATH, 'EqualEqual_ObjectObject'); g.link(('pawn.CombatPawn', 'samepawn.A'), ('currentpawn.ReturnValue', 'samepawn.B'))
    g.branch('same'); g.link(('samepawn.ReturnValue', 'same.condition'), ('psalive.then', 'same.exec'))
    g.branch('gmalive'); g.link((_valid(g, 'gmvalid', 'manager.CombatGM'), 'gmalive.condition'), ('same.then', 'gmalive.exec'))
    g.get('closed', 'CombatClosed', GM); g.link(('manager.CombatGM', 'closed.self')); g.branch('managerclosed'); g.link(('gmalive.then', 'managerclosed.exec'), ('closed.CombatClosed', 'managerclosed.condition'))
    g.get('savedasc', 'CombatASC'); g.call('sameasc', MATH, 'EqualEqual_ObjectObject'); g.link(('savedasc.CombatASC', 'sameasc.A'), ('asc.ReturnValue', 'sameasc.B'))
    g.call('samehealth', MATH, 'EqualEqual_ObjectObject'); g.link(('survivor.CombatSurvivor', 'samehealth.A'), ('findhealth.ReturnValue', 'samehealth.B'))
    g.call('samebindings', MATH, 'BooleanAND'); g.link(('sameasc.ReturnValue', 'samebindings.A'), ('samehealth.ReturnValue', 'samebindings.B'))
    g.branch('bindingscurrent'); g.link(('managerclosed.else', 'bindingscurrent.exec'), ('samebindings.ReturnValue', 'bindingscurrent.condition'))
    g.call('destroy', ACTOR, 'K2_DestroyActor')
    for p in ('checkready.else', 'pawnalive.else', 'psalive.else', 'same.else', 'gmalive.else', 'managerclosed.then', 'bindingscurrent.else'): g.link((p, 'destroy.exec'))
    g.event('end', 'ReceiveEndPlay', ACTOR); g.seq('cleanup', 2); g.chain('end', 'cleanup')
    g.branch('endhashealth'); g.link(('cleanup.Then_0', 'endhashealth.exec'), ('healthvalid.ReturnValue', 'endhashealth.condition'))
    g.n('unbind', 'removedelegate', **{'class': SURVIVOR, 'delegate': 'OnHealthChanged'})
    g.link(('endhashealth.then', 'unbind.exec'), ('survivor.CombatSurvivor', 'unbind.self'), ('health_event.OutputDelegate', 'unbind.Delegate'))
    g.branch('endhasaction'); g.link(('cleanup.Then_1', 'endhasaction.exec'), (_valid(g, 'actionvalid', 'action.CombatAction'), 'endhasaction.condition'))
    g.call('stopaction', ASYNC_BASE, 'EndAction'); g.link(('endhasaction.then', 'stopaction.exec'), ('action.CombatAction', 'stopaction.self'))
    return g.json()


def gm_logic():
    g = G()
    for n in ('CombatStart', 'CombatReconcile', 'CombatFlush', 'CombatCoverage', 'CombatRoundStarted', 'CombatClose'): g.existing(n, n)
    g.existing('emit', 'CombatEmit')
    g.call('guid', GUID, 'NewGuid'); g.call('guidstr', GUID, 'Conv_GuidToString'); g.link(('guid.ReturnValue', 'guidstr.InGuid'))
    g.set('epochset', 'CombatEpoch'); g.link(('guidstr.ReturnValue', 'epochset.CombatEpoch')); g.chain('CombatStart', 'epochset')
    g.selfnode('self')
    for n, event, period in [('reconcile_timer', 'CombatReconcile', '1.0'), ('flush_timer', 'CombatFlush', '0.2')]:
        g.call(n, SYS, 'K2_SetTimer', {'FunctionName': event, 'Time': period, 'bLooping': 'true'}); g.link(('self.self', n + '.Object'))
    g.chain('epochset', 'reconcile_timer', 'flush_timer')
    # Reconcile synchronously at the native round boundary, as well as on the recovery timer.
    # Warmup may replace pawns; a missing bind at StartRound permanently degrades coverage.
    g.n('round_event', 'createevent', func='CombatReconcile')
    g.n('live_event', 'createevent', func='CombatRoundStarted')
    for node, delegate in [('roundbind', 'OnRoundStarted'), ('warmupbind', 'OnRoundWarmup')]:
        g.adddelegate(node, delegate, BC_GS)
        g.link(('bodygs.cast_result', node + '.self'), (('live_event' if node == 'roundbind' else 'round_event') + '.OutputDelegate', node + '.Delegate'))
    g.call('initialreconcile', None, 'CombatReconcile'); g.chain('flush_timer', 'roundbind', 'warmupbind', 'initialreconcile')
    g.set('boundarybegin', 'CombatBoundary', defaults={'CombatBoundary': 'true'})
    g.call('boundaryreconcile', None, 'CombatReconcile'); g.set('boundaryend', 'CombatBoundary', defaults={'CombatBoundary': 'false'})
    g.chain('CombatRoundStarted', 'boundarybegin', 'boundaryreconcile', 'boundaryend')

    # Enumerate the authoritative PlayerArray, then reconcile each current pawn against observer actors.
    g.call('gs', GS_LIB, 'GetGameState'); g.get('players', 'PlayerArray', GS); g.link(('gs.ReturnValue', 'players.self'))
    g.set('boundzero', 'CombatBound', defaults={'CombatBound': '0'}); g.chain('CombatReconcile', 'boundzero')
    g.foreach('playersloop'); g.link(('players.PlayerArray', 'playersloop.Array')); g.chain('boundzero', 'playersloop')
    g.call('pawn', PS, 'GetPawn'); g.link(('playersloop.Array Element', 'pawn.self'))
    g.branch('pawnvalid'); g.link((_valid(g, 'validpawn', 'pawn.ReturnValue'), 'pawnvalid.condition'), ('playersloop.LoopBody', 'pawnvalid.exec'))
    g.set('foundzero', 'CombatFound', defaults={'CombatFound': 'false'}); g.link(('pawnvalid.then', 'foundzero.exec'))
    g.call('observers', GS_LIB, 'GetAllActorsOfClass', {'ActorClass': OBSERVER}); g.chain('foundzero', 'observers')
    g.foreach('observerloop'); g.link(('observers.OutActors', 'observerloop.Array')); g.chain('observers', 'observerloop')
    g.cast('observer', OBSERVER, pure=True); g.link(('observerloop.Array Element', 'observer.cast_object'))
    g.get('observed', 'CombatPawn', OBSERVER); g.link(('observer.cast_result', 'observed.self'))
    g.call('same', MATH, 'EqualEqual_ObjectObject'); g.link(('pawn.ReturnValue', 'same.A'), ('observed.CombatPawn', 'same.B'))
    g.branch('match'); g.link(('observerloop.LoopBody', 'match.exec'), ('same.ReturnValue', 'match.condition'))
    g.set('foundyes', 'CombatFound', defaults={'CombatFound': 'true'}); g.link(('match.then', 'foundyes.exec'))
    g.get('ready', 'CombatReady', OBSERVER); g.link(('observer.cast_result', 'ready.self')); g.branch('boundready')
    g.link(('ready.CombatReady', 'boundready.condition')); g.chain('foundyes', 'boundready')
    _inc(g, 'bound', 'CombatBound', 'boundready.then')
    g.get('found', 'CombatFound'); g.branch('needobserver'); g.link(('found.CombatFound', 'needobserver.condition'), ('observerloop.Completed', 'needobserver.exec'))
    g.spawn('spawn', OBSERVER); g.link(('needobserver.else', 'spawn.exec'))
    g.call('transform', MATH, 'MakeTransform'); g.link(('transform.ReturnValue', 'spawn.SpawnTransform'))
    g.call('observe', OBSERVER, 'CombatObserve'); g.link(('spawn.ReturnValue', 'observe.self'), ('pawn.ReturnValue', 'observe.Pawn'), ('self.self', 'observe.Manager')); g.chain('spawn', 'observe')

    g.call('rosterlen', ARR, 'Array_Length', array=True); g.link(('players.PlayerArray', 'rosterlen.TargetArray'))
    g.get('boundcount', 'CombatBound'); g.get('gaps', 'CombatGaps')
    g.get('active', 'CombatActive'); g.get('closed', 'CombatClosed')
    g.call('allbound', MATH, 'EqualEqual_IntInt'); g.link(('boundcount.CombatBound', 'allbound.A'), ('rosterlen.ReturnValue', 'allbound.B'))
    g.call('nonempty', MATH, 'Greater_IntInt', {'B': '0'}); g.link(('rosterlen.ReturnValue', 'nonempty.A'))
    g.call('full', MATH, 'BooleanAND'); g.link(('allbound.ReturnValue', 'full.A'), ('nonempty.ReturnValue', 'full.B'))
    g.call('live', STR, 'EqualEqual_StrStr', {'B': 'Game.Phase.StartRound'}); g.link(('phasestr.ReturnValue', 'live.A'))
    g.get('boundary', 'CombatBoundary'); g.call('islive', MATH, 'BooleanOR'); g.link(('boundary.CombatBoundary', 'islive.A'), ('live.ReturnValue', 'islive.B'))
    g.call('endmatch', STR, 'EqualEqual_StrStr', {'B': 'Game.Phase.EndMatch'}); g.link(('phasestr.ReturnValue', 'endmatch.A'))
    g.branch('checkfull'); g.link(('playersloop.Completed', 'checkfull.exec'), ('full.ReturnValue', 'checkfull.condition'))
    g.branch('incomplete_live'); g.link(('checkfull.else', 'incomplete_live.exec'), ('islive.ReturnValue', 'incomplete_live.condition'))
    missing = _inc(g, 'missing', 'CombatGaps', 'incomplete_live.then')
    g.call('checkcoverage', None, 'CombatCoverage'); g.link(('checkfull.then', 'checkcoverage.exec'), ('incomplete_live.else', 'checkcoverage.exec'), (missing, 'checkcoverage.exec'))

    # Warmup can contain a fully bound host-only roster. Do not start coverage until the
    # first native round boundary; reconcile already counts any missing bindings there.
    g.branch('alreadyactive'); g.link(('CombatCoverage.then', 'alreadyactive.exec'), ('active.CombatActive', 'alreadyactive.condition'))
    g.branch('canactivate'); g.link(('alreadyactive.else', 'canactivate.exec'), ('boundary.CombatBoundary', 'canactivate.condition'))
    g.set('activate', 'CombatActive', defaults={'CombatActive': 'true'}); g.link(('canactivate.then', 'activate.exec'))
    g.branch('isclosed'); g.link(('closed.CombatClosed', 'isclosed.condition'), ('alreadyactive.then', 'isclosed.exec'))
    g.branch('ending'); g.link(('isclosed.else', 'ending.exec'), ('endmatch.ReturnValue', 'ending.condition'))
    g.call('reportable', MATH, 'BooleanOR'); g.link(('full.ReturnValue', 'reportable.A'), ('islive.ReturnValue', 'reportable.B'))
    g.branch('coveragevisible'); g.link(('ending.else', 'coveragevisible.exec'), ('reportable.ReturnValue', 'coveragevisible.condition'))
    g.call('nogaps', MATH, 'EqualEqual_IntInt', {'B': '0'}); g.link(('gaps.CombatGaps', 'nogaps.A'))
    g.call('completeok', MATH, 'BooleanAND'); g.link(('nogaps.ReturnValue', 'completeok.A'), ('full.ReturnValue', 'completeok.B'))
    g.call('completebit', MATH, 'SelectString', {'A': '1', 'B': '0'}); g.link(('completeok.ReturnValue', 'completebit.bPickA'))
    coverage = _row(g, 'cov', [('kind=coverage;observer=chcombat-1;complete=0;damage=1;shots=0;objectives=0;roster=', _string(g, 'rosterstr', 'rosterlen.ReturnValue')),
                             (';bound=', _string(g, 'boundstr', 'boundcount.CombatBound')), (';gaps=', _string(g, 'gapsstr', 'gaps.CombatGaps'))])
    g.call('coverage', None, 'CombatEmit'); g.link((coverage, 'coverage.Row'), ('activate.then', 'coverage.exec'), ('coveragevisible.then', 'coverage.exec'))
    closing = _row(g, 'closing', [('kind=coverage;terminal=1;observer=chcombat-1;damage=1;shots=0;objectives=0;complete=', 'completebit.ReturnValue'),
                                (';roster=', 'rosterstr.ReturnValue'), (';bound=', 'boundstr.ReturnValue'), (';gaps=', 'gapsstr.ReturnValue')])
    g.call('finalcoverage', None, 'CombatEmit'); g.link((closing, 'finalcoverage.Row'))
    g.call('closefromphase', None, 'CombatClose'); g.link(('ending.then', 'closefromphase.exec'))
    g.branch('closeonce'); g.link(('closed.CombatClosed', 'closeonce.condition')); g.chain('CombatClose', 'closeonce')
    g.set('closeactive', 'CombatActive', defaults={'CombatActive': 'true'}); g.link(('closeonce.else', 'closeactive.exec')); g.chain('closeactive', 'finalcoverage')
    g.set('close', 'CombatClosed', defaults={'CombatClosed': 'true'}); g.chain('finalcoverage', 'close')
    g.call('stopreconcile', SYS, 'K2_ClearTimer', {'FunctionName': 'CombatReconcile'}); g.link(('self.self', 'stopreconcile.Object')); g.chain('close', 'stopreconcile')

    # Capture event metadata before queuing. Delayed transmission must not rewrite round/phase/time.
    g.branch('acceptactive'); g.link(('emit.then', 'acceptactive.exec'), ('active.CombatActive', 'acceptactive.condition'))
    g.branch('acceptopen'); g.link(('acceptactive.then', 'acceptopen.exec'), ('closed.CombatClosed', 'acceptopen.condition'))
    after = _inc(g, 'sequence', 'CombatSeq', 'acceptopen.else')
    g.get('seq', 'CombatSeq'); g.get('epoch', 'CombatEpoch'); g.get('queue', 'CombatQueue')
    g.call('time', GS_LIB, 'GetTimeSeconds')
    g.cast('bodygs', BC_GS, pure=True); g.link(('gs.ReturnValue', 'bodygs.cast_object'))
    g.call('round', BC_GS, 'GetCurrentRound'); g.call('phase', BC_GS, 'GetCurrentPhase')
    for n in ('round', 'phase'): g.link(('bodygs.cast_result', n + '.self'))
    g.call('tagname', TAG, 'GetTagName'); g.link(('phase.ReturnValue', 'tagname.GameplayTag'))
    payload = _row(g, 'envelope', [('v=1;seq=', _string(g, 'seqstr', 'seq.CombatSeq')), (';epoch=', 'epoch.CombatEpoch'),
                                  (';n=', _string(g, 'roundstr', 'round.ReturnValue')), (';t=', _string(g, 'timestr', 'time.ReturnValue', 'Double')),
                                  (';phase=', _string(g, 'phasestr', 'tagname.ReturnValue', 'Name')), (';', 'emit.Row')])
    g.call('qlen', ARR, 'Array_Length', array=True); g.link(('queue.CombatQueue', 'qlen.TargetArray'))
    g.call('terminal', STR, 'StartsWith', {'InPrefix': 'kind=coverage;terminal=1;'}); g.link(('emit.Row', 'terminal.SourceString'))
    g.call('reserved', MATH, 'SelectInt', {'A': str(QUEUE_LIMIT), 'B': str(QUEUE_LIMIT-1)}); g.link(('terminal.ReturnValue', 'reserved.bPickA'))
    g.call('capacity', MATH, 'Less_IntInt'); g.link(('qlen.ReturnValue', 'capacity.A'), ('reserved.ReturnValue', 'capacity.B'))
    g.branch('room'); g.link((after, 'room.exec'), ('capacity.ReturnValue', 'room.condition'))
    g.call('enqueue', ARR, 'Array_Add', array=True); g.link(('room.then', 'enqueue.exec'), ('queue.CombatQueue', 'enqueue.TargetArray'), (payload, 'enqueue.NewItem'))
    _inc(g, 'overflow', 'CombatGaps', 'room.else')

    g.event('end', 'ReceiveEndPlay', ACTOR)
    g.branch('endgs'); g.link((_valid(g, 'validgs', 'bodygs.cast_result'), 'endgs.condition')); g.chain('end', 'endgs')
    for node, delegate in [('roundunbind', 'OnRoundStarted'), ('warmupunbind', 'OnRoundWarmup')]:
        g.n(node, 'removedelegate', **{'class': BC_GS, 'delegate': delegate})
        g.link(('bodygs.cast_result', node + '.self'), (('live_event' if node == 'roundunbind' else 'round_event') + '.OutputDelegate', node + '.Delegate'))
    g.link(('endgs.then', 'roundunbind.exec')); g.chain('roundunbind', 'warmupunbind')
    g.chain('stopreconcile', 'endgs')
    return g.json()


FUNCTIONS = [('ResolveCombatActor', resolve_signature, resolve_logic), ('ResolveCombatCauser', causer_signature, causer_logic)]
GRAPH_EXPORTS = (gm_events, resolve_signature, resolve_logic, causer_signature, causer_logic, observer_events, observer_logic, gm_logic)
