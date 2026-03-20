import json, sys
params = json.load(sys.stdin)
result = {k.lower(): v if k != list(params.keys())[0] else k.capitalize() for k, v in params.items()}
print(result)