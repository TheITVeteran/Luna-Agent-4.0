import json, sys
params = json.load(sys.stdin)
result = {k: params[k] for k in sorted(params.keys(), reverse=True)}
print(result)