import json, sys
params = json.load(sys.stdin)

def capitalize_keys(params):
    result = {k.capitalize(): v for k, v in params.items()}
    return result

print(json.dumps(capitalize_keys(params), indent=2))