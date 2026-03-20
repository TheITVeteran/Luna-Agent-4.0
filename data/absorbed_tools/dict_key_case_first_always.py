import json, sys
params = json.load(sys.stdin)

result = {k.upper(): v if k == list(params.keys())[0] else k.lower() for k,v in params.items()}
print(result)