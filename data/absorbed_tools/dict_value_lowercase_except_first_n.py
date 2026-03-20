import json, sys
params = json.load(sys.stdin)

result = {}
for key, value in params.items():
    if isinstance(value, int) and value < len(params):
        result[key] = [param for k, param in params.items() if k != key][:value] + list(map(str.lower, params.values()))[value+1:] + [params[key]]
    else:
        result[key] = str(value).lower()

print(json.dumps(result))