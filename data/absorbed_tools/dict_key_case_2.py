import json, sys
params = json.load(sys.stdin)
result = {k.lower(): v for k, v in params.items()}
print(json.dumps(result))