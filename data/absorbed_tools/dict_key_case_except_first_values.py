import json, sys
params = json.load(sys.stdin)
result = {k: v.lower() if i != 0 else v for i, (k, v) in enumerate(params.items())}
print(result)