import json, sys
params = json.load(sys.stdin)
result = {k: v for k, v in params.items() if isinstance(v, str)}
if isinstance(result, dict):
    result = {k.lower(): v for k, v in result.items()}
print(json.dumps(result, indent=4))