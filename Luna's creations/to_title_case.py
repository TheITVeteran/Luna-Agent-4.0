import json, sys
params = json.load(sys.stdin)
result = [item.title() for item in params]
print(result)