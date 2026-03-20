import json, sys
params = json.load(sys.stdin)
result = {k: v for k, v in params.items()}
print(result)