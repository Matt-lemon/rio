"""Optional input adapter, with no executable entry point or runtime imports.

The application supplies its reader and main loop. Importing this module does
not load nas_main, contact an LLM, or turn on Home execution.
"""

from .tools import parse_light_request, user_message


def run_chat(module, registry):
    original = module.read_user_input

    def read_with_home(prompt="You: "):
        while True:
            text = original(prompt)
            intent = parse_light_request(text)
            if intent is None:
                return text
            answer = registry.call("home_set_light", intent)
            print("Rio: " + user_message(answer) + "\n")

    module.read_user_input = read_with_home
    try:
        module.main()
    finally:
        module.read_user_input = original
