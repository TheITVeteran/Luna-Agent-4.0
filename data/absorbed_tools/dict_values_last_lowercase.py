import json, sys
params = json.load(sys.stdin)
result = {k.lower(): v[-1] for k, v in params.items()}
print(json.dumps(result))