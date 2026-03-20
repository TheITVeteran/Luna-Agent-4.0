import json, sys
params = json.load(sys.stdin)
result = {k.lower(): v.lower() for k, v in params.items()}
print(result)