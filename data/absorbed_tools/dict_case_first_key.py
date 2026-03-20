import json, sys
params = json.load(sys.stdin)
result = {k.lower(): v if k != next(iter(params)) else params[k] for k, v in params.items()}
print(result)