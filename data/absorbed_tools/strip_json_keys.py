import json, sys
params = json.load(sys.stdin)
result = {v: params.pop(k) for k in params if v != params}
print(result)