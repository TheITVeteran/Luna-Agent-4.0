import json, sys
params = json.load(sys.stdin)
result = len(params.values())
print(result)