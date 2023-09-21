#!/usr/bin/python

import io
import sys
import os
import yaml
from toscaparser.tosca_template import ToscaTemplate

def _merge_templates(template, new_template):
    for item in ["inputs", "node_templates", "outputs"]:
        if item in new_template["topology_template"]:
            if item not in template["topology_template"]:
                template["topology_template"][item] = {}
            template["topology_template"][item].update(new_template["topology_template"][item])
    return template

directory = sys.argv[1]

for path, _, files in os.walk(directory):
    for name in files:
        with io.open(os.path.join(path, name)) as stream:
            print("Template: " + name)
            template = yaml.full_load(stream)
            if "metadata" in template and "parents" in template["metadata"]:
                for parent in template["metadata"]["parents"]:
                    print("Parent: " + parent)
                    with io.open(os.path.abspath(os.path.join(path, "..", "templates", parent))) as pstream:
                        parent_template = yaml.full_load(pstream)
                        full_template = _merge_templates(template, parent_template)
                        ToscaTemplate(yaml_dict_tpl=full_template)
