import json, sys
params = json.load(sys.stdin)
if isinstance(params, dict):
    result = {k: [v.lower()] + (value.lower() if not isinstance(value, list) else value) for k, value in params.items()}
else:
    result = None
print(json.dumps(result))