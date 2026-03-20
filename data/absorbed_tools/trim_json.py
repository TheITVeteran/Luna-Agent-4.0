import json, sys
params = json.load(sys.stdin)
for k, v in params.items():
    for i in range(len(v)):
        if not v[i].isspace():
            v = v[i:]
print(params)