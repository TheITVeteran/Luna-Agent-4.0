import json, sys
params = json.load(sys.stdin)

def convert_value(val):
    if isinstance(val, set): return val
    elif isinstance(val, list) and all(isinstance(x, set) for x in val): return val
    else: return str(val).lower()

result = {k: convert_value(v) for k, v in params.items()}
print(json.dumps(result))