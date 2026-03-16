import json, sys
params = json.load(sys.stdin)
result = [item.lower() for item in params]
print(result)