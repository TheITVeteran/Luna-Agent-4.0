import json, sys
params = json.load(sys.stdin)
result = {k: v.capitalize() if isinstance(v, str) else v for k, v in params.items()}
print(json.dumps(result))