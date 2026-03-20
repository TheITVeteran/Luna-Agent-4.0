import json, sys
import re

params = json.load(sys.stdin)
result = {k.lower(): v if ' ' not in v else v for k, v in params.items()}
print(result)