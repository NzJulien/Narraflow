"""NarraFlow Studio: the voice-first illustrated storytelling engine.

Voice (AssemblyAI Voice Agent API) is the interface, the structured story
model is the intelligence, and illustration is the output. The voice agent
never touches this package directly: the browser executes the agent's tool
calls against the REST layer in `routes.py`, which calls `tools.py`.
"""
