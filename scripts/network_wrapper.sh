#!/bin/bash
# Copyright (C) SchedMD LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


update_file() {
    local path="$1"
    local data="$2"

   if [[ -z "$path" || -z "$data" ]]; then
       echo "Error: func_update_fstab requires two arguments: path and data"
       return 1
   fi

    # Create JSON data
    local json_data='{"path": "'"${path}"'", "data": "'"${data}"'"}'

    # Run Ansible playbook and use network_storage.yml.
    ansible-playbook network_storage.yml -e "$json_data"
    local exit_status=$?
    if [[ $exit_status -ne 0 ]]; then
        echo "ansible playbook failed with exit code $exit_status"
        return $exit_status
    fi

    echo "ansible updated file successfully"
}

run_cmd() {
    local command="$1"
    shift  # Remove the command itself from the arguments

    if [[ -z "$command" ]]; then
        echo "Error: run_cmd requires at least one argument (the command to run)"
        return 1
    fi

    echo "Running command: $command $@"
    "$command" "$@"  # Execute the command with remaining arguments
    local exit_status=$?
    if [[ $exit_status -ne 0 ]]; then
        echo "Command failed with exit code $exit_status"
        return $exit_status  # Propagate error
    fi
}

if [[ "$1" == "update_file" ]]; then
    shift
    update_file "$@"
elif [[ "$1" == "run_cmd" ]]; then
    shift
    run_cmd "$@"
else
    echo "Usage: $0 {update_file|run_cmd} [arguments...]"
    echo "  update_file: file_name line_to_add"
    echo "  run_cmd: command [arg1] [arg2] ..."
fi
