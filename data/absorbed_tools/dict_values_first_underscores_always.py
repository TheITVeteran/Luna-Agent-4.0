import json, sys
params = json.load(sys.stdin)

def convert_to_underscore(s):
    return s.replace(' ', '_').lower()

result = {k: convert_to_underscore(v) for k, v in params.items()}
print(result)