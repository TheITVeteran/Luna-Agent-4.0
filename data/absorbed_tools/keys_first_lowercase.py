import json, sys
params = json.load(sys.stdin)
keys = sorted(list(params.keys()))
print(' '.join(key.lower() for key in keys))