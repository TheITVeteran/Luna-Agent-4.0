import json, sys
params = json.load(sys.stdin)
result = next((v for v in params.values() if v), None)
print(result)