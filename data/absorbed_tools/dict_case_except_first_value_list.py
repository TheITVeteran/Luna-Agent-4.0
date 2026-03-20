import json, sys
params = json.load(sys.stdin)
result = {k: v[1:] if len(v) > 1 else v for k, v in params.items()}
print(json.dumps(result))