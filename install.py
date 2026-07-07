import os
import venv

# 1. Virtual Environment
venv_dir = os.path.join(os.getcwd(), "venv")
venv.EnvBuilder(with_pip = True).create(venv_dir)

