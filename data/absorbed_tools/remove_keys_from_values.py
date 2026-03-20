import json, sys
params = json.load(sys.stdin)
result = {k: v for k, v in params.items() if k not in ['key1', 'key2']}
print(result)