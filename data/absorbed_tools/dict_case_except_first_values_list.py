import json, sys
params = json.load(sys.stdin)
result = [to_dict_case(value) if i > 0 else value for i, value in enumerate(params.values())]
print(json.dumps(result))