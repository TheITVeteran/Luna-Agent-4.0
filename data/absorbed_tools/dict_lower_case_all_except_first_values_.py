import json, sys
params = json.load(sys.stdin)

result = {k: [v.lower() if not isinstance(v, list) else v] if isinstance(v, list) else v for k, v in params.items()}
print(result)