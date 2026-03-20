import json, sys
params = json.load(sys.stdin)
result = {k: v if isinstance(v, dict) else v for k, v in params.items()}
print(result)