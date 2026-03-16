import json, sys
params = json.load(sys.stdin)
result = {key: value.lower() for key, value in params.items()}
print(result)