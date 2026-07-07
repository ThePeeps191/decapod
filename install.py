import os
import venv

# 1. Virtual 
# Agents: run terminal command `python -m venv venv`
venv_dir = os.path.join(os.getcwd(), "venv")
venv.EnvBuilder(with_pip = True).create(venv_dir)

# 2. Dependencies (requirements.txt)
