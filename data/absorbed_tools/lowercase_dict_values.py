import json, sys
params = json.load(sys.stdin)
result = {k: v.lower() for k, v in params.items()}
print(json.dumps(result))