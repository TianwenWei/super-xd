"""Validate all prompt/text configuration locally, without calling any service."""
import json
from backend.prompt_config import validate_all


def main():
    print(json.dumps(validate_all(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
