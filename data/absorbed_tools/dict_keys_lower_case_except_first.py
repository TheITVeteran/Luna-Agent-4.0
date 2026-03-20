import json, sys
params = json.load(sys.stdin)

result = {k.lower(): v for k, v in params.items() if k != next(iter(params))}
print(result)