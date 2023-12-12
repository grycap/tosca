#!/usr/bin/python

import io
import sys
import os
import yaml

parameters_directory = sys.argv[1]
templates_directory = sys.argv[2]

for path, _, files in os.walk(parameters_directory):
    for name in files:
        with io.open(os.path.join(path, name)) as stream:
            print("Parameters file: " + name)
            parameters = yaml.full_load(stream)

        ymlf = os.path.join(templates_directory,"%s.yml" % os.path.splitext(name)[0][:-11])
        yamlf = os.path.join(templates_directory, "%s.yaml" % os.path.splitext(name)[0][:-11])
        template_file = None
        print(ymlf)
        if os.path.exists(ymlf):
            template_file = ymlf
        elif os.path.exists(yamlf):
            template_file = yamlf
        
        if template_file is None:
            print("Template file not found for parameters file: " + name)
            sys.exit(1)

        with io.open(template_file) as stream:
            print("Template file: " + name)
            template = yaml.full_load(stream)

        for key, value in parameters["inputs"].items():
            if "tab" not in value:
                print("Parametes input without tab")
                sys.exit(1)
            elif value["tab"] not in parameters["tabs"]:
                print("Tab %s not found in parameters file" % value["tab"])
                sys.exit(1)
            if key not in template.get("topology_template", {}).get("inputs", {}):
                print("Parameter input %s not found in template" % key)
                sys.exit(1)
