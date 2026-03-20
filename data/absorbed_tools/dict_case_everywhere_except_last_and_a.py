import json, sys
params = json.load(sys.stdin)

def case_convert(d):
    if isinstance(d, dict):
        return {k.lower(): v for k, v in d.items()[:-1]}
    elif isinstance(d, list):
        return [x.lower() if i != len(x) - 1 else x for i, x in enumerate(d)]
    elif isinstance(d, str):
        return d.lower()
    else:
        return d

result = {k: case_convert(v) for k, v in params.items()}
print(json.dumps(result))