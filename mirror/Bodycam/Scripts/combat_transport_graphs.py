"""Bounded acknowledged combat batches; each HTTP attempt owns its delegate actor."""
from ctf_graphs import G, P, SYS, MATH, STR, GS_LIB, ARR, ACTOR
from combat_graphs import GM, DIR, _inc

REQUEST = DIR + '/BP_CHCombatRequest.BP_CHCombatRequest_C'
VARIABLES = [('CombatBatchRows', 'string', None, 'array'), ('CombatBatch', 'string', None, None),
             ('CombatBatchCount', 'int', None, None), ('CombatAttempt', 'int', None, None),
             ('CombatSentAt', 'double', None, None)]


def request_events():
    g = G()
    g.custom('start', 'StartRequest', [P('OwnerManager', 'object', **{'class': GM}), P('RequestAttempt', 'int'), P('Payload', 'string')])
    g.custom('reply', 'OnResponse', [P('Success', 'bool')])
    return g.json()


def request_logic():
    from bb5_graphs import _report_send, REPORT_URL
    g = G(); g.existing('start', 'StartRequest'); g.existing('reply', 'OnResponse')
    after = 'start'
    for name, source in [('Manager', 'start.OwnerManager'), ('Attempt', 'start.RequestAttempt')]:
        g.set('set' + name, name); g.link((source, 'set' + name + '.' + name)); g.chain(after, 'set' + name); after = 'set' + name
    g.call('life', ACTOR, 'SetLifeSpan', {'InLifespan': '12.0'}); g.chain(after, 'life')
    _report_send(g, 'http', {'URL': REPORT_URL + '/batch', 'EventName': 'ch_combat_batch', 'FirstSessionTimestamp': 'chbatch-1'})
    g.link(('start.Payload', 'http.Platform')); g.n('delegate', 'createevent', func='OnResponse'); g.link(('delegate.OutputDelegate', 'http.OnResponse')); g.chain('life', 'http')
    g.get('manager', 'Manager'); g.get('attempt', 'Attempt')
    g.call('valid', SYS, 'IsValid'); g.link(('manager.Manager', 'valid.Object'))
    g.branch('safe'); g.link(('valid.ReturnValue', 'safe.condition')); g.chain('reply', 'safe')
    g.call('forward', GM, 'CombatBatchReply'); g.link(('manager.Manager', 'forward.self'), ('attempt.Attempt', 'forward.Attempt'), ('reply.Success', 'forward.Success')); g.chain('safe', 'forward')
    return g.json()


def manager_logic():
    g = G(); g.existing('flush', 'CombatFlush'); g.existing('reply', 'CombatBatchReply'); g.selfnode('self')
    for name in ('CombatInFlight', 'CombatSentAt', 'CombatBatchCount', 'CombatBatch', 'CombatBatchRows', 'CombatQueue', 'CombatAttempt'):
        g.get(name, name)
    g.call('time', GS_LIB, 'GetTimeSeconds')
    g.call('elapsed', MATH, 'Subtract_DoubleDouble'); g.link(('time.ReturnValue', 'elapsed.A'), ('CombatSentAt.CombatSentAt', 'elapsed.B'))
    g.call('expired', MATH, 'GreaterEqual_DoubleDouble', {'B': '10.0'}); g.link(('elapsed.ReturnValue', 'expired.A'))
    g.call('idle', MATH, 'Not_PreBool'); g.link(('CombatInFlight.CombatInFlight', 'idle.A'))
    g.call('available', MATH, 'BooleanOR'); g.link(('idle.ReturnValue', 'available.A'), ('expired.ReturnValue', 'available.B'))
    g.branch('can'); g.link(('available.ReturnValue', 'can.condition')); g.chain('flush', 'can')
    g.call('hasbatch', MATH, 'Greater_IntInt', {'B': '0'}); g.link(('CombatBatchCount.CombatBatchCount', 'hasbatch.A'))
    g.branch('frozen'); g.link(('hasbatch.ReturnValue', 'frozen.condition')); g.chain('can', 'frozen')
    g.foreach('collect'); g.link(('CombatQueue.CombatQueue', 'collect.Array'), ('frozen.else', 'collect.Exec'))
    g.call('prefix', MATH, 'EqualEqual_IntInt'); g.link(('collect.Array Index', 'prefix.A'), ('CombatBatchCount.CombatBatchCount', 'prefix.B'))
    g.call('slots', MATH, 'Less_IntInt', {'B': '16'}); g.link(('CombatBatchCount.CombatBatchCount', 'slots.A'))
    g.call('row', STR, 'Concat_StrStr'); g.link(('CombatBatch.CombatBatch', 'row.A'), ('collect.Array Element', 'row.B'))
    g.call('line', STR, 'Concat_StrStr', {'B': '\n'}); g.link(('row.ReturnValue', 'line.A'))
    g.call('length', STR, 'Len'); g.link(('line.ReturnValue', 'length.S'))
    g.call('fits', MATH, 'LessEqual_IntInt', {'B': '1800'}); g.link(('length.ReturnValue', 'fits.A'))
    g.call('both', MATH, 'BooleanAND'); g.link(('prefix.ReturnValue', 'both.A'), ('slots.ReturnValue', 'both.B'))
    g.call('all', MATH, 'BooleanAND'); g.link(('both.ReturnValue', 'all.A'), ('fits.ReturnValue', 'all.B'))
    g.branch('include'); g.link(('all.ReturnValue', 'include.condition'), ('collect.LoopBody', 'include.exec'))
    g.set('append', 'CombatBatch'); g.link(('line.ReturnValue', 'append.CombatBatch')); g.chain('include', 'append')
    g.call('retain', ARR, 'Array_Add', array=True); g.link(('CombatBatchRows.CombatBatchRows', 'retain.TargetArray'), ('collect.Array Element', 'retain.NewItem')); g.chain('append', 'retain')
    _inc(g, 'count', 'CombatBatchCount', 'retain.then')
    g.branch('nonempty'); g.link(('hasbatch.ReturnValue', 'nonempty.condition'), ('collect.Completed', 'nonempty.exec'))
    after = _inc(g, 'attempt', 'CombatAttempt', 'nonempty.then')
    g.link(('frozen.then', 'attemptset.exec'))
    g.set('sent', 'CombatSentAt'); g.link(('time.ReturnValue', 'sent.CombatSentAt'), (after, 'sent.exec'))
    g.set('busy', 'CombatInFlight', defaults={'CombatInFlight': 'true'}); g.chain('sent', 'busy')
    g.call('transform', MATH, 'MakeTransform'); g.spawn('request', REQUEST); g.link(('transform.ReturnValue', 'request.SpawnTransform')); g.chain('busy', 'request')
    g.call('start', REQUEST, 'StartRequest'); g.link(('request.ReturnValue', 'start.self'), ('self.self', 'start.OwnerManager'), ('CombatAttempt.CombatAttempt', 'start.RequestAttempt'), ('CombatBatch.CombatBatch', 'start.Payload')); g.chain('request', 'start')
    g.call('same', MATH, 'EqualEqual_IntInt'); g.link(('reply.Attempt', 'same.A'), ('CombatAttempt.CombatAttempt', 'same.B'))
    g.call('current', MATH, 'BooleanAND'); g.link(('same.ReturnValue', 'current.A'), ('CombatInFlight.CombatInFlight', 'current.B'))
    g.branch('accept'); g.link(('current.ReturnValue', 'accept.condition')); g.chain('reply', 'accept')
    g.set('unbusy', 'CombatInFlight', defaults={'CombatInFlight': 'false'}); g.chain('accept', 'unbusy')
    g.branch('success'); g.link(('reply.Success', 'success.condition')); g.chain('unbusy', 'success')
    g.foreach('acked'); g.link(('CombatBatchRows.CombatBatchRows', 'acked.Array')); g.chain('success', 'acked')
    g.call('remove', ARR, 'Array_Remove', {'IndexToRemove': '0'}, array=True); g.link(('CombatQueue.CombatQueue', 'remove.TargetArray'), ('acked.LoopBody', 'remove.exec'))
    g.call('clearrows', ARR, 'Array_Clear', array=True); g.link(('CombatBatchRows.CombatBatchRows', 'clearrows.TargetArray'), ('acked.Completed', 'clearrows.exec'))
    g.set('cleartext', 'CombatBatch', defaults={'CombatBatch': ''}); g.set('clearcount', 'CombatBatchCount', defaults={'CombatBatchCount': '0'}); g.chain('clearrows', 'cleartext', 'clearcount')
    return g.json()
