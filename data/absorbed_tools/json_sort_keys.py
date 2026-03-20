import json, sys
params = json.load(sys.stdin)
result = str({k: v for k, v in sorted(params.items())})
print(result)