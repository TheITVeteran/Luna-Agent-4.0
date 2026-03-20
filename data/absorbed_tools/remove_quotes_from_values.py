import json, sys
params = json.load(sys.stdin)
result = {k: v.strip('"') for k, v in params.items()}
print(json.dumps(result, indent=4))