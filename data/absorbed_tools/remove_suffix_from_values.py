import json, sys
params = json.load(sys.stdin)
result = {key: value.rstrip('_suffix') if key.endswith('_suffix') else value for key, value in params.items()}
print(json.dumps(result))