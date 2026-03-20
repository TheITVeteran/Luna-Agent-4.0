import json, sys
params = json.load(sys.stdin)
result = {key.upper(): val for key, val in params.items()}
print(result)