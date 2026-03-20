import json, sys
params = json.load(sys.stdin)
result = {k.lower().replace('_', ''): v for k, v in params.items()}
print(result)