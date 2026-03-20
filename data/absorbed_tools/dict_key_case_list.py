import json, sys
params = json.load(sys.stdin)
result = [to_lower(key) for key in params.keys()]
print(result)