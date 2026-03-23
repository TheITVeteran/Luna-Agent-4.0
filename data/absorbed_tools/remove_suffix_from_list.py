import json, sys
params = json.load(sys.stdin)
result = [v.split('-', 1)[-1] for v in params.values() if '-' in params.values()]
print(result)