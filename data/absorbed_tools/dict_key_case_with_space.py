import json, sys
params = json.load(sys.stdin)
result = {k.lower().replace(' ', '') : v for k, v in params.items()}
print(result)