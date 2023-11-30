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
            if item == "inputs":
                for k,v in new_template["topology_template"]["inputs"].items():
                    if k not in template["topology_template"]["inputs"]:
                        template["topology_template"]["inputs"][k] = v
                    else:
                        template["topology_template"]["inputs"][k].update(v)
            else:
                template["topology_template"][item].update(new_template["topology_template"][item])
    return template

directory = sys.argv[1]

for path, _, files in os.walk(directory):
    for name in files:
        with io.open(os.path.join(path, name)) as stream:
            print("Template: " + name)
            template = yaml.full_load(stream)
        # Check if all child templates are present
        if "metadata" in template and "childs" in template["metadata"]:
            for child in template["metadata"]["childs"]:
                if not os.path.isfile(os.path.abspath(os.path.join(path, "..", "templates", child))):
                    raise Exception("Child template %s not found" % child)
        # Check all child templates meging with parents
        if "metadata" in template and "parents" in template["metadata"]:
            if "link" in template["metadata"]:
                raise Exception("Child template cannot have link metadata")
            # Check if the template has set a name and a display name
            if "name" not in template["metadata"]:
                raise Exception("Child template must have a name")
            if "display_name" not in template["metadata"]:
                raise Exception("Child template must have a display name")
            for parent in template["metadata"]["parents"]:
                print("Parent: " + parent)
                with io.open(os.path.abspath(os.path.join(path, "..", "templates", parent))) as pstream:
                    parent_template = yaml.full_load(pstream)
                    full_template = _merge_templates(parent_template, template)
                ToscaTemplate(yaml_dict_tpl=full_template)
        # Test also link templates
        if "metadata" in template and "link" in template["metadata"]:
            if "parents" in template["metadata"]:
                raise Exception("Link template cannot have parents")
            parent = template["metadata"]["link"]["parent"]
            with io.open(os.path.join(path, parent)) as stream:
                print("Link Parent: " + parent)
                full_template = yaml.full_load(stream)
            for child in template["metadata"]["link"]["childs"]:
                print("Link Child: " + child)
                with io.open(os.path.abspath(os.path.join(path, "..", "templates", child))) as cstream:
                    child_template = yaml.full_load(cstream)
                    full_template = _merge_templates(full_template, child_template)
            ToscaTemplate(yaml_dict_tpl=full_template)
