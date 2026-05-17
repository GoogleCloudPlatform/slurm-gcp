#!/bin/bash
echo "$@"
source ../.venv/bin/activate
exec ansible-playbook "$@"
