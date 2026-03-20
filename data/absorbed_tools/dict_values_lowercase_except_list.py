import json, sys
params = json.load(sys.stdin)

def convert_to_lowercase(params):
    result = {k: [str(v).lower()] if isinstance(v, list) else str(v).lower() for k, v in params.items()}
    return result

result = convert_to_lowercase(params)
print(json.dumps(result))