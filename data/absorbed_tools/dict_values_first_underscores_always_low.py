import json, sys
params = json.load(sys.stdin)
result = {k: v.lower().replace('_', '') for k, v in params.items()}
print(json.dumps(result))