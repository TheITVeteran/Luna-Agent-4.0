import json, sys
params = json.load(sys.stdin)

def dict_key_case_ignore_case(json_obj):
    return {key.lower(): val for key, val in json_obj.items()}

result = dict_key_case_ignore_case(params)
print(result)