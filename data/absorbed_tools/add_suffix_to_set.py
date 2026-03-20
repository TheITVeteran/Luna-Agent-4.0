import json, sys
params = json.load(sys.stdin)
result = set(str(item) + '_suffix' for item in params)
print(result)