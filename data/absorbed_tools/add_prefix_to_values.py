import json, sys
params = json.load(sys.stdin)

prefix = params.get('prefix')
except_values = params.get('except_values', [])
result = {key: (value + prefix if value not in except_values else value) for key, value in params.items()}
print(json.dumps(result))