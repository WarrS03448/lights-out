"""Test-only Redis bridge. Run with Python containing fakeredis[lua]."""
import json
import sys
import fakeredis

db = fakeredis.FakeRedis(decode_responses=True)
for line in sys.stdin:
    try:
        request = json.loads(line)
        result = db.execute_command(*request['command'])
        if request['command'][0] == 'PING' and result is True:
            result = 'PONG'
        elif request['command'][0] == 'SET' and result is True:
            result = 'OK'
        elif isinstance(result, set):
            result = sorted(result)
        print(json.dumps({'id': request['id'], 'result': result}), flush=True)
    except Exception as error:
        print(json.dumps({'id': request.get('id'), 'error': str(error)}), flush=True)
