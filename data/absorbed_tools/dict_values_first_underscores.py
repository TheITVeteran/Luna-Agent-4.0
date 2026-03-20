import json, sys
params = json.load(sys.stdin)
result = {k.replace('.', '_'): v for k, v in params.items()}
print(result)