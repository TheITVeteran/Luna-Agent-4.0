import json, sys
params = json.load(sys.stdin)
result = {k.lower(): v if i == len(params.keys()) - 1 else params[k] for i, k in enumerate(params)}
print(result)