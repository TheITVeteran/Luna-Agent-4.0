import json, sys
params = json.load(sys.stdin)
result = {k: v + '_suffix' for k, v in params.items()}
print(json.dumps(result))