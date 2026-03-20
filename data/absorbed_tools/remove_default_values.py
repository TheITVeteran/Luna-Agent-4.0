import json, sys
params = json.load(sys.stdin)
for key, value in params.items():
    if value == {} or value == ['null'] or value == [] or value == '[]':
        del params[key]
print(json.dumps(params))