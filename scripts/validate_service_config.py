"""Validate service choices without starting clients or making network requests."""
if __name__ == "__main__":
    from backend.local_environment import load_local_environment
    load_local_environment()

import json

from backend.services.config import DOMAINS, load_services_config


def main():
    config = load_services_config()
    profiles = {domain: config.profile(domain)['provider'] for domain in DOMAINS if domain != 'media'}
    profiles['media'] = config.media_bindings()
    roles = {name: config.role(name)['profile'] for name in ('talker', 'planner', 'thinker', 'finalizer')}
    print(json.dumps({'ok': True, 'source': config.source, 'profiles': profiles, 'llm_roles': roles}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
