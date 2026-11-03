"""Check configuration offline; --probe explicitly sends a small paid API request."""
import argparse
import sys
from dotenv import load_dotenv
from ai_provider import AIClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    try:
        client = AIClient()
    except (ValueError, OSError, TypeError) as exc:
        # JSON parsing errors can contain user configuration: do not print it.
        print("Invalid AI configuration or missing key. Check AI_CONFIG and provider environment variables.")
        return 1
    print(f"Provider: {client.config.name}; model: {client.model}; protocol: {client.config.protocol}")
    if args.probe:
        answer = client.complete([{"role": "user", "content": "Reply with OK"}], max_tokens=32)
        if answer.startswith("ОШИБКА"):
            print(answer)
            return 1
        print("AI endpoint returned a nonempty response.")
    else:
        print("Configuration valid. Network was not contacted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
