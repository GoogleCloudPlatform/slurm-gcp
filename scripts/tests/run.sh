#!/bin/bash

cd scripts/tests
pytest -W ignore::DeprecationWarning
