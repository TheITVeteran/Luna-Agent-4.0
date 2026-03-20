import json, sys
params = json.load(sys.stdin)
result = {key.lower(): value.lower() if not key.startswith('la') else value for key, value in params.items()}
print(result)