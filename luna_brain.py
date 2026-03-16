"""Luna brain: decide whether to remember / add core. Stub returns no-op dict."""

def brain_step(scope: str, text: str, context: dict | None = None) -> dict:
    """Return dict with should_add_core, should_remember (bool). Stub: both False."""
    return {"should_add_core": False, "should_remember": False}


def brain_should_remember(scope: str, text: str) -> bool:
    """Whether the brain suggests remembering this. Stub: False."""
    return False
