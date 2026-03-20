import json, sys
params = json.load(sys.stdin)

for key, value in params.items():
    if isinstance(value, dict):
        print(key, value)
    else:
        print(key, value.lower())