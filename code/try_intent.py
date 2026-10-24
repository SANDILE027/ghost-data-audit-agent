"""Try the intent classifier on your own text.

    python3 code/try_intent.py "Please delete my account"
    python3 code/try_intent.py            # interactive: type a request, Enter; empty line quits
    python3 code/try_intent.py --llm "..." # also ask Claude (needs ANTHROPIC_API_KEY)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ghost_audit.intent import LLMIntentClassifier, rule_intent  # noqa: E402
from ghost_audit.usage import UsageTracker  # noqa: E402


def show(text, llm):
    r = rule_intent(text)
    verdict = "DELETION -> audit it" if r["authorizes_deletion"] else "NOT A DELETION -> NOT_APPLICABLE []"
    print(f"\n  rules : {verdict}")
    print(f"          deletion cues : {', '.join(r['cues']) or '-'}")
    print(f"          negations     : {', '.join(r['negations']) or '-'}")
    print(f"          other purpose : {', '.join(r['other']) or '-'}")
    if llm is not None:
        out = llm.classify(text)
        if out is None:
            print("  claude: unavailable (set ANTHROPIC_API_KEY and pip install anthropic)")
        else:
            print(f"  claude: authorizes_deletion={out['authorizes_deletion']} type={out.get('request_type')} "
                  f"lang={out.get('language')} ({out['method']})")
            if out["authorizes_deletion"] != r["authorizes_deletion"]:
                print("  !! rules and Claude disagree")
    print()


def main():
    args = sys.argv[1:]
    use_llm = "--llm" in args
    args = [a for a in args if a != "--llm"]
    llm = LLMIntentClassifier(os.path.join(HERE, "cache", "intent_cache.json"), UsageTracker()) if use_llm else None
    if args:
        show(" ".join(args), llm)
    else:
        print("Type a request (any language). Empty line to quit.")
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                break
            if not text:
                break
            show(text, llm)
    if llm is not None:
        llm.save()


if __name__ == "__main__":
    main()
