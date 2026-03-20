import json, sys
params = json.load(sys.stdin)
result = {key.lower(): value for key, value in params.items()}
print(result)