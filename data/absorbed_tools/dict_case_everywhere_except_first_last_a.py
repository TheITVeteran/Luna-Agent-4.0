import json, sys, functools

params = json.load(sys.stdin)
result = {k: [v.lower() for v in sorted([x.lower() for x in str(v).split()])] if len(str(v)) > 1 else [str(v).lower()] for k, v in params.items()}
print(json.dumps(result, indent=4))