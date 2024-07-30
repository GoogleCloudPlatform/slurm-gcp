#!/bin/bash
echo "$@"
source `pipenv --venv`/bin/activate
exec ansible-playbook "$@"
