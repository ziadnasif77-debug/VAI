"""AI layer (SPEC §13).

Modular by requirement: no model is hardcoded, and replacing one must not break
the application. Each capability is reached through a provider interface, so a
model swap is a configuration change.
"""
