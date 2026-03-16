import json, sys
params = json.load(sys.stdin)
result = len(set([item for item in params if isinstance(item, str)]))
print(result)