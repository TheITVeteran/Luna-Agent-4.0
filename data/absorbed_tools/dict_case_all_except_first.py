import json, sys
params = json.load(sys.stdin)
result = {key: params[key] if i == 0 else params[key].case() for i, key in enumerate(params)}
print(json.dumps(result, indent=2))