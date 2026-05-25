import os


def load_env():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, '.env'),                 # alongside this module
        os.path.join(os.path.dirname(here), '.env') # one level up (project root when env_loader is in lib/)
    ):
        if os.path.exists(candidate):
            env_path = candidate
            break
    else:
        return
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
