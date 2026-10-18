"""Validate all prompt/text configuration locally, without calling any service."""
if __name__ == "__main__":
    from super_xd.demo.environment import load_local_environment
    load_local_environment()

import json
from super_xd.nlp.prompts.config import validate_all


def main():
    print(json.dumps(validate_all(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
