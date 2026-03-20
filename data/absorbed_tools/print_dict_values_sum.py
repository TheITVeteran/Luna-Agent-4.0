import json, sys
params = json.load(sys.stdin)
result = sum(params.values())
print(result)