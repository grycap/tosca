#!/usr/bin/python

import io
import sys
import os
import yaml
import re

templates_directory = sys.argv[1    ]

for path, _, files in os.walk(templates_directory):
    for name in files:
        with io.open(os.path.join(path, name)) as stream:
            print("Template file: " + name)
            template = yaml.full_load(stream)
            template_inputs = template.get("topology_template", {}).get("inputs", {})

            tabs = template.get('metadata', {}).get('tabs', {})
            for tab, input_elems in tabs.items():
                if not isinstance(tab, str):
                    raise Exception("Tab name must be a string")
                # Special case for a regex to select inputs
                if isinstance(input_elems, str):
                    all_inputs = list(template_inputs.keys())
                    res = [elem for elem in all_inputs if re.match(input_elems, elem)]
                    input_elems = res
                elif not isinstance(input_elems, list):
                    print("Invalid type for input %s." % input_elems)
                    sys.exit(1)
                if not input_elems:
                    print("Tab %s has no inputs" % tab)
                    sys.exit(1)
                for input_elem in input_elems:
                    input_name = input_elem
                    if isinstance(input_elem, dict):
                        input_name = list(input_elem.keys())[0]
                        input_params = list(input_elem.values())[0]
                        if not isinstance(input_params, dict):
                            print("Invalid type for input parameters %s." % input_name)
                            sys.exit(1)
                    if input_name not in template_inputs:
                        print("Tab input %s not found in template" % input_name)
                        sys.exit(1)
