import json, sys
params = json.load(sys.stdin)
result = json.dumps(params, indent=4)
print(result)