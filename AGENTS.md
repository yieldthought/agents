# AGENTS

This project favors simple, direct code over layered abstractions or rich typing.

## Style notes
- Prefer minimal interfaces and conventions over type-heavy APIs.
- Keep classes small and focused; avoid unnecessary generics or helpers.
- Use clear, human-readable docstrings to define contracts and behavior.
- Favor simple return types (e.g., `None`/string for checkers) over complex objects.
- Avoid overengineering: implement the simplest thing that works and reads cleanly.
- Keep defaults sensible and avoid extra parameters unless they add real value.
- Avoid use of '*' in python argument lists and don't use function(thing=thing, other=other) syntax unnecessarily when function(thing, other) is already self-descibing.
- Clarity and conciseness are king. The best lines are the ones that aren't written.
- It's good to be robust to external APIs failing, e.g. error if file exists, but for our own code we should be *defining* what it does and should definitely not be writing code that is robust to e.g. an internal API function not fulfilling its contract or having multiple confusing return types when one would do.
- Remember the Zen of Python.
