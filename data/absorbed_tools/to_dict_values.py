import json, sys
params = json.load(sys.stdin)
result = {f'item_{i}': value for i, value in enumerate(params)}
print(result)