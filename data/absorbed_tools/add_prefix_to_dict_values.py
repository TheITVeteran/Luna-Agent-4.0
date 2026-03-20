import json, sys
params = json.load(sys.stdin)
result = {k: v + '_prefixed' for k, v in params.items()}
print(json.dumps(result))