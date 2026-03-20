import json, sys
params = json.load(sys.stdin)
if 'list' in params and len(params['list']) > 0:
    result = {k: v[:1] if isinstance(v, list) else v for k, v in params.items()}
else:
    result = params
print(json.dumps(result))