import json, sys
params = json.load(sys.stdin)

result = {k: [v.lower()] if isinstance(v, str) else v for k, v in params.items()}
print(result)