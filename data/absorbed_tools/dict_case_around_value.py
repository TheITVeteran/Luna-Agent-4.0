import json, sys
params = json.load(sys.stdin)
result = {key: value if key.islower() else key.upper() for key, value in params.items()}
print(result)