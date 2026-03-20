import json, sys
params = json.load(sys.stdin)
result = {k: v.upper() for k, v in params.items()}
print(result)