import json, sys
params = json.load(sys.stdin)
result = ', '.join(sorted(params.keys()))
print(result)