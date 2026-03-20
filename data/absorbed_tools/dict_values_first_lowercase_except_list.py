import json, sys
params = json.load(sys.stdin)

def lowerexceptlist(d):
    return {k:v.lower() if not isinstance(v, list) else v for k,v in d.items()}
result = lowerexceptlist(params)
print(result)