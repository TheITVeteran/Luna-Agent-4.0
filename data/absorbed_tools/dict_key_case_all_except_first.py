import json, sys
params = json.load(sys.stdin)
result = {k[1 if i == 0 else len(k) - 1]: v for i, k in enumerate(params)}
print(result)