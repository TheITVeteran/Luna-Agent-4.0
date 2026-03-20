import json, sys
params = json.load(sys.stdin)
result = [item.lower() for item in params if isinstance(params, list)]
print(result)