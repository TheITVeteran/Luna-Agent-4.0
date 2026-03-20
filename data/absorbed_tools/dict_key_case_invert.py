import json, sys
params = json.load(sys.stdin)

invert_dict_keys = {k.lower(): v for k, v in params.items()}

print(invert_dict_keys)