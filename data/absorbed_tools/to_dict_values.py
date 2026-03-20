import json, sys
params = json.load(sys.stdin)
result = {k: str(v) for k, v in params.items()}
print(result)