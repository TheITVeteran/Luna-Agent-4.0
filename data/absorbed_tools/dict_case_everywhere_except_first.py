import json, sys
params = json.load(sys.stdin)
result = {k: params[k].casefold() if k != next(iter(params)) else params[k] for k in params}
print(result)