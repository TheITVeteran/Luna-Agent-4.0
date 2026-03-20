import json, sys
params = json.load(sys.stdin)
result = {k: v[0] if isinstance(v, (list, tuple)) else v for k, v in params.items()}
print(json.dumps(result, indent=4))