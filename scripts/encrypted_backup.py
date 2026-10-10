"""Compatibility entry point for promoted encrypted snapshots."""
import sys
from app.storage import encrypted_backup as _implementation
sys.modules[__name__] = _implementation
