import json, sys
params = json.load(sys.stdin)
for key in params.keys():
    for value in [params[key]]:
        if isinstance(value, str):
            value = value.replace('"', '')
print(params)