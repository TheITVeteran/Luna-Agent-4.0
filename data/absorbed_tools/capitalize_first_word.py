import json, sys
params = json.load(sys.stdin)
result = {k: v.capitalize() if v else '' for k, v in params.items()}
print(json.dumps(result))