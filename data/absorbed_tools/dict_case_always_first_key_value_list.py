import json, sys
params = json.load(sys.stdin)
result = [(k.upper(), v.lower()) for k, v in params.items()]
print(json.dumps(result))