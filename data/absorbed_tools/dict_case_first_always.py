import json, sys
params = json.load(sys.stdin)
dict_case_first_always = lambda d, prefix='': {k.lower(): v if k.startswith(prefix) else k.upper() if not k else k for k, v in d.items()}
print(json.dumps(dict_case_first_always(params)))