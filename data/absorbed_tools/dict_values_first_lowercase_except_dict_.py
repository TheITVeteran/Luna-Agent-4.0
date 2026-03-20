import json, sys
params = json.load(sys.stdin)
result = {k: v.lower() if type(v) != dict else v for k, v in params.items()}
print(result)