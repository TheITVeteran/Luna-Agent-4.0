import json, sys
params = json.load(sys.stdin)
result = {k: [v[0].lower(), v[-1].upper()] if len(v) > 1 else v for k, v in params.items()}
print(result)